from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Iterable

from api.session.models import RecentReference, utcnow


DEFAULT_CONFIDENCE_THRESHOLD = 0.8
_RECENCY_AMBIGUITY_SECONDS = 60
_TIME_PATTERN = re.compile(
    r"改到\s*(?P<period>上午|早上|下午|晚上|中午)?\s*(?P<hour>[0-9一二三四五六七八九十]+)\s*點"
)
_CHINESE_HOURS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ReferenceCandidate:
    reference: RecentReference
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference.to_dict(),
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReferenceResolution:
    text: str
    status: str
    confidence: float
    candidates: tuple[ReferenceCandidate, ...] = ()
    selected: ReferenceCandidate | None = None
    proposed_update: dict[str, object] | None = None
    time_candidates: tuple[str, ...] = ()
    reason: str = ""

    @property
    def requires_clarification(self) -> bool:
        return self.status != "resolved"

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "status": self.status,
            "confidence": self.confidence,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selected": self.selected.to_dict() if self.selected else None,
            "proposed_update": dict(self.proposed_update) if self.proposed_update else None,
            "time_candidates": list(self.time_candidates),
            "reason": self.reason,
        }


def _active_references(references: Iterable[RecentReference], now: datetime) -> list[RecentReference]:
    return sorted(
        (item for item in references if not item.is_expired(now)),
        key=lambda item: item.created_at,
        reverse=True,
    )


def _candidates(items: list[RecentReference], reason: str) -> tuple[ReferenceCandidate, ...]:
    if not items:
        return ()
    if len(items) == 1:
        return (ReferenceCandidate(items[0], 0.98, reason),)
    gap = (items[0].created_at - items[1].created_at).total_seconds()
    top_confidence = 0.55 if gap <= _RECENCY_AMBIGUITY_SECONDS else 0.9
    return tuple(
        ReferenceCandidate(item, top_confidence if index == 0 else max(0.1, top_confidence - 0.35 - index * 0.05), reason)
        for index, item in enumerate(items)
    )


def _from_candidates(
    text: str,
    candidates: tuple[ReferenceCandidate, ...],
    *,
    threshold: float,
    update: dict[str, object] | None = None,
    time_candidates: tuple[str, ...] = (),
    reason: str,
) -> ReferenceResolution:
    if len(time_candidates) > 1:
        return ReferenceResolution(
            text=text,
            status="ambiguous",
            confidence=0.0,
            candidates=candidates,
            time_candidates=time_candidates,
            reason="time_ambiguous",
        )
    if not candidates:
        return ReferenceResolution(text=text, status="unresolved", confidence=0.0, time_candidates=time_candidates, reason=reason)
    top = candidates[0]
    if len(candidates) == 1 or (top.confidence >= threshold and top.confidence > candidates[1].confidence):
        if top.confidence >= threshold:
            return ReferenceResolution(
                text=text,
                status="resolved",
                confidence=top.confidence,
                candidates=candidates,
                selected=top,
                proposed_update=dict(update or {}),
                time_candidates=time_candidates,
                reason=reason,
            )
    return ReferenceResolution(
        text=text,
        status="ambiguous",
        confidence=top.confidence,
        candidates=candidates,
        time_candidates=time_candidates,
        reason=reason,
    )


def _ambiguous(
    text: str,
    candidates: tuple[ReferenceCandidate, ...],
    *,
    time_candidates: tuple[str, ...] = (),
    reason: str,
) -> ReferenceResolution:
    return ReferenceResolution(
        text=text,
        status="ambiguous",
        confidence=candidates[0].confidence if candidates else 0.0,
        candidates=candidates,
        time_candidates=time_candidates,
        reason=reason,
    )


def _parse_hour(raw: str) -> int | None:
    if raw.isdigit():
        value = int(raw)
    else:
        value = _CHINESE_HOURS.get(raw)
    return value if value is not None and 1 <= value <= 12 else None


def _time_candidates(text: str) -> tuple[str, ...]:
    match = _TIME_PATTERN.search(text)
    if not match:
        return ()
    hour = _parse_hour(match.group("hour"))
    if hour is None:
        return ()
    period = match.group("period") or ""
    if period in {"下午", "晚上"}:
        return (f"{(hour % 12) + 12:02}:00",)
    if period == "中午":
        return (f"{hour % 12 + 12:02}:00",)
    if period in {"上午", "早上"}:
        return (f"{hour % 12:02}:00",)
    morning = f"{hour % 12:02}:00"
    afternoon = f"{hour % 12 + 12:02}:00"
    return (morning, afternoon)


def resolve_reference(
    text: str,
    references: Iterable[RecentReference],
    *,
    active_case_id: str | None = None,
    active_schedule_id: str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    now: datetime | None = None,
) -> ReferenceResolution:
    """Resolve a short Chinese reference without changing session state.

    A caller may apply ``proposed_update`` only when ``status`` is ``resolved``.
    Every ambiguous or unresolved result deliberately returns no selected target
    and no proposed update.
    """

    normalized = str(text or "").strip()
    if not normalized:
        return ReferenceResolution(text="", status="unresolved", confidence=0.0, reason="empty_reference")
    if not 0 < confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")
    items = _active_references(references, _as_utc(now or utcnow()))

    if "剛才那件" in normalized or "剛剛那件" in normalized:
        candidates = _candidates([item for item in items if item.kind == "case"], "latest_case")
        return _from_candidates(
            normalized,
            candidates,
            threshold=confidence_threshold,
            update={"case_id": candidates[0].reference.item_id} if candidates else None,
            reason="latest_case",
        )

    if "同一案件" in normalized:
        cases = [item for item in items if item.kind == "case"]
        if active_case_id:
            active = [item for item in cases if item.item_id == active_case_id]
            if active:
                candidate = ReferenceCandidate(active[0], 1.0, "active_case")
                return ReferenceResolution(
                    text=normalized,
                    status="resolved",
                    confidence=1.0,
                    candidates=(candidate,),
                    selected=candidate,
                    proposed_update={"case_id": active_case_id},
                    reason="active_case",
                )
        candidates = _candidates(cases, "same_case")
        if len(candidates) > 1:
            return _ambiguous(normalized, candidates, reason="same_case_ambiguous")
        return _from_candidates(
            normalized,
            candidates,
            threshold=confidence_threshold,
            update={"case_id": candidates[0].reference.item_id} if candidates else None,
            reason="same_case",
        )

    times = _time_candidates(normalized)
    if times:
        schedules = [item for item in items if item.kind == "schedule"]
        if active_schedule_id:
            active = [item for item in schedules if item.item_id == active_schedule_id]
            candidates = (ReferenceCandidate(active[0], 1.0, "active_schedule"),) if active else ()
        else:
            candidates = _candidates(schedules, "recent_schedule")
        if len(candidates) > 1:
            return _ambiguous(
                normalized,
                candidates,
                time_candidates=times,
                reason="schedule_target_ambiguous",
            )
        update = {"schedule_id": candidates[0].reference.item_id, "start_time": times[0]} if candidates and len(times) == 1 else None
        return _from_candidates(
            normalized,
            candidates,
            threshold=confidence_threshold,
            update=update,
            time_candidates=times,
            reason="schedule_change",
        )

    return ReferenceResolution(text=normalized, status="unresolved", confidence=0.0, reason="unsupported_reference")
