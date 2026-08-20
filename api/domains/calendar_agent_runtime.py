"""Conversation and database adapter for the safe calendar agent flow."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
import re
from typing import Any, Protocol

from api.domains.calendar_agent import (
    CalendarDraft,
    CalendarEvent,
    CalendarIntent,
    check_event_safety,
    confirm_draft,
    event_from_draft,
    parse_calendar_request,
)
from api.domains.calendar_metadata import decode_calendar_source, encode_calendar_source


PENDING_KEY = "calendar_agent_draft"
_CALENDAR_NOUN_RE = re.compile(r"(?:行事曆|日曆|行程|開會|會議|排庭|庭期|約診|預約|提醒)")
_CALENDAR_ACTION_RE = re.compile(r"(?:新增|建立|加入|安排|排定|預約|提醒|查詢|查看|查一下|有哪些|空檔|空閒|修改|變更|延後|提前|取消|刪除|移除)")
_RECURRENCE_RE = re.compile(r"每(?:週|周|星期|月)")


class CalendarRepository(Protocol):
    def list_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        hint: str = "",
        mutable_only: bool = False,
    ) -> list[CalendarEvent]: ...

    def create_event(self, event: CalendarEvent) -> CalendarEvent: ...

    def update_event(self, event_id: str, event: CalendarEvent) -> CalendarEvent: ...

    def cancel_event(self, event_id: str) -> bool: ...


def looks_like_calendar_request(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if _CALENDAR_NOUN_RE.search(text) and (_CALENDAR_ACTION_RE.search(text) or re.search(r"(?:今天|明天|後天|本週|這週|下週|\d{1,4}[年./-]\d{1,2})", text)):
        return True
    return bool(_RECURRENCE_RE.search(text) and re.search(r"(?:會|課|提醒|繳|訪|庭|行程)", text))


def _draft_to_dict(draft: CalendarDraft, *, target_id: str = "") -> dict[str, Any]:
    return {
        "intent": draft.intent.value,
        "source_text": draft.source_text,
        "title": draft.title,
        "start": draft.start.isoformat() if draft.start else "",
        "end": draft.end.isoformat() if draft.end else "",
        "all_day": draft.all_day,
        "rrule": draft.rrule or "",
        "target_hint": draft.target_hint,
        "target_id": target_id,
        "query_start": draft.query_start.isoformat() if draft.query_start else "",
        "query_end": draft.query_end.isoformat() if draft.query_end else "",
        "missing_fields": list(draft.missing_fields),
        "ambiguities": list(draft.ambiguities),
        "assumptions": list(draft.assumptions),
        "errors": list(draft.errors),
        "timezone": draft.timezone,
    }


def _draft_from_dict(payload: dict[str, Any]) -> tuple[CalendarDraft, str]:
    def _dt(key: str) -> datetime | None:
        value = str(payload.get(key) or "").strip()
        return datetime.fromisoformat(value) if value else None

    draft = CalendarDraft(
        intent=CalendarIntent(str(payload.get("intent") or CalendarIntent.UNKNOWN.value)),
        source_text=str(payload.get("source_text") or ""),
        title=str(payload.get("title") or ""),
        start=_dt("start"),
        end=_dt("end"),
        all_day=bool(payload.get("all_day")),
        rrule=str(payload.get("rrule") or "") or None,
        target_hint=str(payload.get("target_hint") or ""),
        query_start=_dt("query_start"),
        query_end=_dt("query_end"),
        missing_fields=tuple(str(item) for item in payload.get("missing_fields") or ()),
        ambiguities=tuple(str(item) for item in payload.get("ambiguities") or ()),
        assumptions=tuple(str(item) for item in payload.get("assumptions") or ()),
        errors=tuple(str(item) for item in payload.get("errors") or ()),
        timezone=str(payload.get("timezone") or "Asia/Taipei"),
    )
    return draft, str(payload.get("target_id") or "")


def _session_id(user_id: str, platform: str) -> str:
    return f"{str(platform or 'unknown').strip().lower()}:{str(user_id or '').strip()}"


def _pending_payload(orch: Any, session_id: str) -> dict[str, Any] | None:
    if not hasattr(orch, "_ensure_runtime_foundations"):
        return None
    orch._ensure_runtime_foundations()
    state = orch._session_store.get_pending_state(session_id)
    value = state.values.get(PENDING_KEY) if state else None
    return dict(value) if isinstance(value, dict) else None


def _set_pending(orch: Any, session_id: str, payload: dict[str, Any]) -> None:
    orch._ensure_runtime_foundations()
    state = orch._session_store.get_pending_state(session_id)
    values = dict(state.values) if state else {}
    values[PENDING_KEY] = dict(payload)
    orch._session_store.set_pending_state(session_id, values)


def _clear_pending(orch: Any, session_id: str) -> None:
    orch._ensure_runtime_foundations()
    state = orch._session_store.get_pending_state(session_id)
    if not state:
        return
    values = dict(state.values)
    values.pop(PENDING_KEY, None)
    if values:
        orch._session_store.set_pending_state(session_id, values)
    else:
        orch._session_store.clear_pending_state(session_id)


def _format_when(event: CalendarEvent) -> str:
    if event.all_day:
        last_day = (event.end - timedelta(days=1)).date()
        if last_day == event.start.date():
            return f"{event.start:%Y/%m/%d}（全天）"
        return f"{event.start:%Y/%m/%d} 至 {last_day:%Y/%m/%d}（全天）"
    return f"{event.start:%Y/%m/%d %H:%M} 至 {event.end:%H:%M}"


def _event_lines(events: list[CalendarEvent], *, limit: int = 12) -> str:
    if not events:
        return "查無符合的行程。"
    lines = [f"共找到 {len(events)} 筆行程："]
    for index, event in enumerate(events[:limit], 1):
        recurrence = "；重複行程" if event.rrule else ""
        lines.append(f"{index}. {event.title}｜{_format_when(event)}{recurrence}")
    if len(events) > limit:
        lines.append(f"另有 {len(events) - limit} 筆未顯示，請縮小日期或名稱範圍。")
    return "\n".join(lines)


def _clarification_text(draft: CalendarDraft) -> str:
    labels = {
        "intent": "要建立、查詢、修改或取消行程",
        "start_date": "日期",
        "start_time": "開始時間（若為全天，請明確說全天）",
        "title": "行程名稱",
        "target_event": "要修改或取消的行程名稱",
        "new_schedule": "新的日期與時間",
        "year_assumed": "年份",
        "multiple_events_may_match": "更具體的行程名稱或時間",
        "recurrence_weekday_unspecified": "每週星期幾",
        "recurrence_monthday_unspecified": "每月幾日",
        "invalid_date": "合法日期",
        "end_before_start": "晚於開始時間的結束時間",
    }
    needed = [labels.get(item, item) for item in (*draft.missing_fields, *draft.ambiguities, *draft.errors)]
    unique = list(dict.fromkeys(needed))
    return "我還不能安全建立行程，請補充：" + "、".join(unique) + "。"


def _preview_text(draft: CalendarDraft, *, conflicts: list[CalendarEvent] | None = None) -> str:
    action = {
        CalendarIntent.CREATE: "建立",
        CalendarIntent.MODIFY: "修改",
        CalendarIntent.CANCEL: "取消",
    }[draft.intent]
    if draft.intent is CalendarIntent.CANCEL:
        body = f"行程：{draft.target_hint}"
    else:
        event = event_from_draft(draft) if draft.intent is CalendarIntent.CREATE else CalendarEvent(
            title=draft.target_hint,
            start=draft.start,
            end=draft.end,
            all_day=draft.all_day,
            rrule=draft.rrule,
        )
        body = f"行程：{draft.title or draft.target_hint}\n時間：{_format_when(event)}"
        if draft.rrule:
            body += f"\n重複規則：{draft.rrule.replace('RRULE:', '')}"
    if conflicts:
        body += "\n時間衝突：" + "、".join(item.title for item in conflicts[:3])
    return f"請確認是否{action}以下行程：\n{body}\n\n請回覆「確認」或「先不要」。"


class OscCalendarRepository:
    """Small adapter over the existing OSC case_todos table."""

    @staticmethod
    def _exec(sql: str, params: tuple[Any, ...] = (), *, fetch: str = "all"):
        from api.osc.utils import _osc_exec

        return _osc_exec(sql, params, fetch=fetch)

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> CalendarEvent:
        day = row.get("todo_date")
        day_value = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        raw_time = row.get("todo_time")
        metadata = decode_calendar_source(row.get("source_file")) or {}
        all_day = bool(metadata.get("all_day")) or not raw_time
        if all_day:
            start = datetime.combine(day_value, time.min)
            fallback_end = start + timedelta(days=1)
        else:
            time_text = str(raw_time)
            parsed_time = time.fromisoformat(time_text)
            start = datetime.combine(day_value, parsed_time)
            fallback_end = start + timedelta(hours=1)
        try:
            end = datetime.fromisoformat(str(metadata.get("end") or ""))
        except ValueError:
            end = fallback_end
        title = str(row.get("description") or row.get("todo_type") or "未命名行程").strip()
        return CalendarEvent(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            rrule=str(metadata.get("rrule") or "") or None,
            event_id=f"todo:{row.get('id')}",
            status=str(row.get("status") or "pending"),
        )

    def list_events(self, *, start=None, end=None, hint="", mutable_only=False) -> list[CalendarEvent]:
        sql = (
            "SELECT id, todo_type, todo_date, todo_time, description, status, source_file "
            "FROM case_todos WHERE todo_date IS NOT NULL "
            "AND (status IS NULL OR status='' OR status!='deleted') "
        )
        params: list[Any] = []
        if start:
            sql += "AND todo_date >= %s "
            params.append(start.date().isoformat())
        if end:
            sql += "AND todo_date < %s "
            params.append(end.date().isoformat())
        if hint:
            sql += "AND (description LIKE %s OR todo_type LIKE %s) "
            like = f"%{hint}%"
            params.extend((like, like))
        if mutable_only:
            sql += "AND source_file LIKE %s "
            params.append("manual_dispatch:calendar_agent:%")
        sql += "ORDER BY todo_date, COALESCE(todo_time, '00:00:00'), id LIMIT 500"
        rows, _ = self._exec(sql, tuple(params), fetch="all")
        return [self._row_to_event(dict(row)) for row in (rows or [])]

    def create_event(self, event: CalendarEvent) -> CalendarEvent:
        source = encode_calendar_source({"end": event.end.isoformat(), "rrule": event.rrule, "all_day": event.all_day})
        todo_time = None if event.all_day else event.start.strftime("%H:%M:%S")
        result, _ = self._exec(
            "INSERT INTO case_todos (case_number, client_name, todo_type, todo_date, todo_time, description, source_file, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("非案件行程", "", "行事曆事件", event.start.date().isoformat(), todo_time, event.title, source, "pending"),
            fetch="none",
        )
        row_id = (result or {}).get("lastrowid") if isinstance(result, dict) else None
        if not row_id:
            row, _ = self._exec(
                "SELECT id FROM case_todos WHERE description=%s AND todo_date=%s AND COALESCE(todo_time,'')=COALESCE(%s,'') AND source_file=%s ORDER BY id DESC LIMIT 1",
                (event.title, event.start.date().isoformat(), todo_time, source),
                fetch="one",
            )
            row_id = (row or {}).get("id") if isinstance(row, dict) else None
        if not row_id:
            raise RuntimeError("calendar_write_verification_failed")
        return replace(event, event_id=f"todo:{row_id}")

    def update_event(self, event_id: str, event: CalendarEvent) -> CalendarEvent:
        row_id = _todo_id(event_id)
        source = encode_calendar_source({"end": event.end.isoformat(), "rrule": event.rrule, "all_day": event.all_day})
        todo_time = None if event.all_day else event.start.strftime("%H:%M:%S")
        result, _ = self._exec(
            "UPDATE case_todos SET todo_date=%s,todo_time=%s,description=%s,source_file=%s,status='pending' WHERE id=%s AND source_file LIKE %s",
            (event.start.date().isoformat(), todo_time, event.title, source, row_id, "manual_dispatch:calendar_agent:%"),
            fetch="none",
        )
        if isinstance(result, dict) and int(result.get("rowcount") or 0) < 1:
            raise RuntimeError("calendar_update_verification_failed")
        return replace(event, event_id=event_id)

    def cancel_event(self, event_id: str) -> bool:
        result, _ = self._exec(
            "UPDATE case_todos SET status='deleted' WHERE id=%s AND source_file LIKE %s",
            (_todo_id(event_id), "manual_dispatch:calendar_agent:%"),
            fetch="none",
        )
        return not isinstance(result, dict) or int(result.get("rowcount") or 0) > 0


def _todo_id(event_id: str) -> int:
    match = re.fullmatch(r"todo:(\d+)", str(event_id or ""))
    if not match:
        raise ValueError("invalid_calendar_event_id")
    return int(match.group(1))


def _matching_targets(repository: CalendarRepository, draft: CalendarDraft) -> list[CalendarEvent]:
    return repository.list_events(
        start=draft.query_start,
        end=draft.query_end,
        hint=draft.target_hint,
        mutable_only=True,
    )


def _execute_confirmed(repository: CalendarRepository, draft: CalendarDraft, target_id: str) -> str:
    if draft.intent is CalendarIntent.CREATE:
        created = repository.create_event(event_from_draft(draft))
        return f"行程已建立並通過回查驗證：{created.title}｜{_format_when(created)}。"
    if draft.intent is CalendarIntent.MODIFY:
        current = repository.list_events(hint=draft.target_hint, mutable_only=True)
        selected = next((item for item in current if item.event_id == target_id), None)
        if not selected:
            return "找不到原行程，未進行修改。"
        updated = CalendarEvent(
            title=selected.title,
            start=draft.start,
            end=draft.end,
            all_day=draft.all_day,
            rrule=draft.rrule or selected.rrule,
            event_id=selected.event_id,
        )
        repository.update_event(target_id, updated)
        return f"行程已修改並通過回查驗證：{updated.title}｜{_format_when(updated)}。"
    if draft.intent is CalendarIntent.CANCEL:
        if repository.cancel_event(target_id):
            return f"行程已取消：{draft.target_hint}。"
        return "找不到可取消的行程，資料未變更。"
    return "這份草稿不是可執行的行事曆變更。"


def handle_calendar_message(
    orch: Any,
    message: str,
    *,
    user_id: str,
    platform: str,
    repository: CalendarRepository | None = None,
    reference_date: date | None = None,
) -> str | None:
    """Handle a calendar turn, returning ``None`` when another route should run."""
    session_id = _session_id(user_id, platform)
    pending = _pending_payload(orch, session_id)
    text = str(message or "").strip()
    repo = repository or OscCalendarRepository()

    if pending:
        draft, target_id = _draft_from_dict(pending)
        decision = confirm_draft(draft, text)
        if decision.accepted is False:
            _clear_pending(orch, session_id)
            return "已取消這次行事曆變更，資料未異動。"
        if decision.accepted is True:
            _clear_pending(orch, session_id)
            return _execute_confirmed(repo, draft, target_id)
        if looks_like_calendar_request(text):
            _clear_pending(orch, session_id)
        else:
            return "目前有一筆行事曆變更等待確認。請回覆「確認」或「先不要」。"

    if not looks_like_calendar_request(text):
        return None
    draft = parse_calendar_request(text, reference_date=reference_date or date.today())
    if draft.intent is CalendarIntent.UNKNOWN:
        return None
    if draft.needs_clarification:
        return _clarification_text(draft)
    if draft.intent is CalendarIntent.QUERY:
        events = repo.list_events(start=draft.query_start, end=draft.query_end, hint=draft.target_hint)
        return _event_lines(events)

    target_id = ""
    if draft.intent in {CalendarIntent.MODIFY, CalendarIntent.CANCEL}:
        matches = _matching_targets(repo, draft)
        if not matches:
            return "找不到可由 MAGI 修改的相符行程，資料未異動。"
        if len(matches) > 1:
            return "找到多筆可能的行程，請加上日期或時間：\n" + _event_lines(matches, limit=8)
        target_id = matches[0].event_id

    conflicts: list[CalendarEvent] = []
    if draft.intent is CalendarIntent.CREATE:
        candidate = event_from_draft(draft)
        existing = repo.list_events(start=candidate.start - timedelta(days=1), end=candidate.end + timedelta(days=1))
        checks = check_event_safety(candidate, existing)
        if checks.has_duplicate:
            return f"相同行程已存在，未重複建立：{candidate.title}｜{_format_when(candidate)}。"
        conflicts = list(checks.conflicts)

    _set_pending(orch, session_id, _draft_to_dict(draft, target_id=target_id))
    return _preview_text(draft, conflicts=conflicts)
