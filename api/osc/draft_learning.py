"""Local correction memory for OSC draft generation.

The module stores human edits as JSONL under .runtime so MAGI can reuse recent
drafting lessons without touching case folders or requiring a DB migration.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / ".runtime" / "osc_draft_learning_events.jsonl"
MAX_TEXT_CHARS = 60000
MAX_NOTE_CHARS = 3000


def _clean_text(value: Any, max_chars: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()[:max_chars]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _one_line(text: str, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _norm_key(text: str) -> str:
    return re.sub(r"[\s　,，、。．.／/\\|｜:：;；()（）【】\[\]「」『』\"']", "", str(text or ""))


def _diff_lessons(original: str, corrected: str, limit: int = 10) -> list[dict]:
    original_lines = [x.strip() for x in original.splitlines() if x.strip()]
    corrected_lines = [x.strip() for x in corrected.splitlines() if x.strip()]
    matcher = difflib.SequenceMatcher(None, original_lines, corrected_lines, autojunk=False)
    lessons: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = " / ".join(original_lines[i1:i2])
        after = " / ".join(corrected_lines[j1:j2])
        if not before and not after:
            continue
        lessons.append({"type": tag, "before": _one_line(before), "after": _one_line(after)})
        if len(lessons) >= limit:
            break
    return lessons


def _line_delta(original: str, corrected: str) -> dict:
    original_lines = [x for x in original.splitlines() if x.strip()]
    corrected_lines = [x for x in corrected.splitlines() if x.strip()]
    return {
        "original_chars": len(original),
        "corrected_chars": len(corrected),
        "original_lines": len(original_lines),
        "corrected_lines": len(corrected_lines),
        "char_delta": len(corrected) - len(original),
        "line_delta": len(corrected_lines) - len(original_lines),
    }


def record_draft_feedback(payload: dict, *, actor: str = "") -> dict:
    original = _clean_text(payload.get("original_text") or payload.get("original") or "")
    corrected = _clean_text(payload.get("corrected_text") or payload.get("corrected") or "")
    note = _clean_text(payload.get("note") or payload.get("feedback_note") or "", MAX_NOTE_CHARS)
    if not original:
        return {"ok": False, "error": "original_text required"}
    if not corrected:
        return {"ok": False, "error": "corrected_text required"}
    if _sha(original) == _sha(corrected) and not note:
        return {"ok": False, "error": "no_change"}

    event = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor": str(actor or "").strip()[:120],
        "case_number": _clean_text(payload.get("case_number") or "", 120),
        "doc_type": _clean_text(payload.get("doc_type") or "", 120),
        "reason": _clean_text(payload.get("reason") or "", 200),
        "provider": _clean_text(payload.get("provider") or "", 80),
        "model": _clean_text(payload.get("model") or "", 120),
        "note": note,
        "stats": _line_delta(original, corrected),
        "lessons": _diff_lessons(original, corrected),
        "original_hash": _sha(original),
        "corrected_hash": _sha(corrected),
        "original_text": original,
        "corrected_text": corrected,
    }
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return {"ok": True, "event": _public_event(event)}


def _iter_events(limit: int = 200) -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    lines = EVENTS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    events = []
    for line in reversed(lines[-max(limit * 3, limit):]):
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            events.append(item)
        if len(events) >= limit:
            break
    return events


def _public_event(event: dict) -> dict:
    public = {k: v for k, v in event.items() if k not in {"original_text", "corrected_text"}}
    public["original_preview"] = _one_line(event.get("original_text") or "", 160)
    public["corrected_preview"] = _one_line(event.get("corrected_text") or "", 160)
    return public


def recent_draft_feedback(limit: int = 20) -> list[dict]:
    return [_public_event(x) for x in _iter_events(limit)]


def draft_learning_summary() -> dict:
    events = _iter_events(300)
    by_doc: dict[str, int] = {}
    by_case: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for event in events:
        doc = str(event.get("doc_type") or "未指定")
        case = str(event.get("case_number") or "未指定")
        reason = str(event.get("reason") or "未指定")
        by_doc[doc] = by_doc.get(doc, 0) + 1
        by_case[case] = by_case.get(case, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "count": len(events),
        "latest_at": events[0].get("created_at") if events else "",
        "by_doc_type": by_doc,
        "by_case_number": by_case,
        "by_reason": by_reason,
    }


def learning_guidance_for_prompt(doc_type: str = "", case_number: str = "", reason: str = "", limit: int = 5) -> str:
    doc = str(doc_type or "").strip()
    case = str(case_number or "").strip()
    reason_key = _norm_key(reason)
    events = _iter_events(120)
    scored: list[tuple[int, float, dict]] = []
    now = time.time()
    for idx, event in enumerate(events):
        event_case = str(event.get("case_number") or "").strip()
        event_reason_key = _norm_key(event.get("reason") or "")
        same_case = bool(case and event_case and event_case == case)
        same_reason = bool(reason_key and event_reason_key and event_reason_key == reason_key)
        if not same_case and not same_reason:
            continue
        score = 0
        if same_case:
            score += 10
        if same_reason:
            score += 8
        if doc and str(event.get("doc_type") or "") == doc:
            score += 4
        score += max(0, 2 - idx // 20)
        scored.append((score, now - idx, event))
    picked = [x[2] for x in sorted(scored, key=lambda x: (-x[0], -x[1]))[:limit]]
    lines = []
    for event in picked:
        label = " / ".join(x for x in [event.get("doc_type"), event.get("case_number")] if x) or "一般書狀"
        note = str(event.get("note") or "").strip()
        if note:
            lines.append(f"- 【{label}】使用者明示：{_one_line(note, 180)}")
        for lesson in (event.get("lessons") or [])[:3]:
            before = str(lesson.get("before") or "").strip()
            after = str(lesson.get("after") or "").strip()
            if before and after:
                lines.append(f"- 【{label}】曾將「{before}」修為「{after}」。")
            elif after:
                lines.append(f"- 【{label}】曾補入「{after}」。")
            elif before:
                lines.append(f"- 【{label}】曾刪除「{before}」。")
            if len(lines) >= limit * 3:
                break
    return "\n".join(lines).strip() or "(尚無人工修正紀錄)"
