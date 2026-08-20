"""Deterministic office-domain understanding before MAGI calls a model.

This module is intentionally side-effect free and cheap enough to run for
every message.  It does not pretend to understand language by itself; it
builds a reviewable contract describing the likely office domain, requested
operation, evidence/tool requirement, and any missing fact that would make a
real assistant stop and ask before acting.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from api.agentic.contracts import Entity, IntentEnvelope, MissingField, SideEffectLevel
from api.routing.intent_contract import classify_intent_contract
from api.tools.policies import ToolRequirement, classify_tool_requirement


_CASE_NUMBER_RE = re.compile(
    r"(?:20\d{2}-\d{4}|1\d{2,3}\u5e74\u5ea6[^\s\uff0c\u3002]{1,24}\u5b57\u7b2c?\s*\d+\s*\u865f|1\d{2,3}\d{4}-[A-Z]-\d{3})",
    re.I,
)
_FILE_NAME_RE = re.compile(r"[^\s\uff0c\u3002\uff1b\uff1f\uff01]{1,100}\.(?:pdf|docx?|xlsx?|txt|md|mp3|m4a|wav|mp4)", re.I)
_TIME_RANGE_RE = re.compile(
    r"(?:\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u5f8c\u5929|\u672c\u9031|\u9019\u9031|\u4e0b\u9031|\u672c\u6708|\u9019\u500b\u6708|\u4e0b\u500b\u6708|"
    r"\d{1,2}[/-]\d{1,2}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}\u6708\d{1,2}\u65e5)",
    re.I,
)
_CLOCK_RE = re.compile(r"(?:\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u665a\u4e0a|\u51cc\u6668)?\s*([\u96f6\u3007\u4e00\u4e8c\u5169\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\d]{1,3})(?:[:\uff1a](\d{2}))?\s*\u9ede")
_VAGUE_RE = re.compile(r"(?:\u9019\u500b|\u90a3\u500b|\u9019\u4efd|\u90a3\u4efd|\u8a72\u4efd|\u525b\u624d|\u525b\u525b|\u524d\u9762|\u4e0a\u9762|\u9019\u4e9b|\u90a3\u4e9b)", re.I)
_MULTIPLE_TARGET_RE = re.compile(r"(?:\u6709\u5169\u4ef6|\u6709\u4e8c\u4ef6|\u6709\u5169\u500b|\u6709\u4e8c\u500b|\u4e0d\u53ea\u4e00\u4ef6|\u591a\u4ef6|\u5169\u6848|\u4e8c\u6848)", re.I)
_GENERIC_HANDLE_RE = re.compile(r"(?:\u5e6b\u6211|\u8acb|\u9ebb\u7169)?.{0,8}(?:\u8655\u7406|\u5f04\u4e00\u4e0b|\u505a\u4e00\u4e0b)", re.I)
_OFFICE_PERSON_RE = re.compile(
    r"(?:[\u4e00-\u9fff]{1,4}(?:\u5148\u751f|\u5c0f\u59d0)(?:\u90a3\u6848|\u9019\u6848|\u7684\u6848)|"
    r"[\u4e00-\u9fff]{2,4}(?:\u90a3\u4ef6|\u9019\u4ef6|\u90a3\u6848|\u9019\u6848|\u7684\u6848\u4ef6|\u7684\u884c\u7a0b|\u7684\u6a94\u6848|\u7684\u5224\u6c7a|\u6848(?=$|[\s\uff0c\u3002\uff1f\uff01])))"
)

_DOMAIN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("case", re.compile(r"(?:\u6848\u4ef6|\u6848\u865f|\u7576\u4e8b\u4eba|\u6848\u7531|\u7d50\u6848)", re.I)),
    ("file", re.compile(r"(?:\u6a94\u6848|\u6587\u4ef6|\u8cc7\u6599\u593e|PDF|DOCX|\u9644\u4ef6|\u5377\u8b49|\u5377\u5b97)", re.I)),
    ("calendar", re.compile(r"(?:\u884c\u7a0b|\u65e5\u66c6|\u958b\u5ead|\u5ead\u671f|\u6cd5\u5ead|\u7684\u5ead|\u6703\u8b70|\u671f\u9650|\u63d0\u9192)", re.I)),
    ("legal_aid", re.compile(r"(?:\u6cd5\u6276|\u6cd5\u5f8b\u6276\u52a9|\u958b\u8fa6|\u5831\u7d50|\u9032\u5ea6\u56de\u5831)", re.I)),
    ("file_review", re.compile(r"(?:\u95b1\u5377|\u96fb\u5b50\u5377\u8b49|\u5377\u5b97\u4e0b\u8f09|\u6cd5\u9662\u5377\u5b97|\u7e73\u8cbb\u55ae|\u7e73\u8cbb\u901a\u77e5)", re.I)),
    ("transcript", re.compile(r"(?:\u7b46\u9304|\u9010\u5b57\u7a3f|\u9304\u97f3|\u8a9e\u97f3\u8f49\u6587\u5b57)", re.I)),
    ("translation", re.compile(r"(?:\u7ffb\u8b6f|\u8b6f\u6587|\u4e2d\u82f1\u5c0d\u7167|\u7ffb\u6210)", re.I)),
    ("summary", re.compile(r"(?:\u6458\u8981|\u7e3d\u7d50|\u91cd\u9ede\u6574\u7406|\u6458\u9304)", re.I)),
    ("legal_research", re.compile(r"(?:\u5224\u6c7a|\u88c1\u5224|\u5be6\u52d9\u898b\u89e3|\u5be6\u52d9|\u6cd5\u689d|\u6cd5\u898f|\u6cd5\u5f8b\u554f\u984c)", re.I)),
    ("drafting", re.compile(r"(?:\u66f8\u72c0|\u8d77\u8a34\u72c0|\u7b54\u8faf\u72c0|\u8072\u8acb\u72c0|\u9673\u5831\u72c0|\u8acb\u5047\u72c0|\u8349\u64ec|\u8d77\u8349)", re.I)),
    ("accounting", re.compile(r"(?:\u5e33\u52d9|\u6536\u5165|\u652f\u51fa|\u85aa\u8cc7|\u734e\u91d1|\u5831\u50f9)", re.I)),
    ("system", re.compile(r"(?:MAGI|\u7cfb\u7d71|\u670d\u52d9|\u5916\u7db2|NAS|\u7d05\u71c8|\u9ec3\u71c8|\u5065\u5eb7)", re.I)),
)

_WRITE_RE = re.compile(r"(?:\u5efa\u7acb|\u65b0\u589e|\u4fee\u6539|\u66f4\u65b0|\u66f4\u6b63|\u4e0a\u50b3|\u540c\u6b65|\u9001\u51fa|\u63d0\u4ea4|\u4e0b\u8f09|\u7522\u751f|\u751f\u6210|\u88fd\u4f5c|\u8349\u64ec|\u8d77\u8349|\u6392\u5b9a|\u56de\u5831|\u5831\u7d50|\u958b\u8fa6|\u6b78\u6a94|\u88dc\u6293|\u6293\u56de|\u88dc\u767b|\u4fee\u5fa9|\u4fee\u597d|\u6392\u9664|\u89f8\u767c)", re.I)
_DESTRUCTIVE_RE = re.compile(r"(?:\u522a\u9664|\u79fb\u9664|\u6e05\u9664|\u5f37\u5236\u8986\u84cb)", re.I)
_LOOKUP_RE = re.compile(r"(?:\u67e5|\u67e5\u8a62|\u627e|\u641c\u5c0b|\u5217\u51fa|\u544a\u8a34\u6211|\u76ee\u524d|\u73fe\u5728|\u72c0\u614b|\u9032\u5ea6|\u662f\u4ec0\u9ebc|\u4f55\u6642|\u591a\u5c11|\u5e7e\u4ef6|\u5e7e\u7b46)", re.I)
_ANALYZE_RE = re.compile(r"(?:\u5206\u6790|\u6bd4\u8f03|\u8a55\u4f30|\u5224\u65b7|\u6458\u8981|\u6574\u7406|\u7814\u7a76)", re.I)

_OFFICE_TOOL_HINTS = {
    "case": "case_query",
    "file": "document_processing",
    "calendar": "calendar_query",
    "legal_aid": "laf_query",
    "file_review": "file_review_query",
    "transcript": "transcript_query",
    "translation": "document_processing",
    "summary": "document_processing",
    "legal_research": "judgment_query",
    "drafting": "drafting_query",
    "accounting": "accounting_query",
    "system": "system_health",
}


@dataclass(frozen=True)
class DomainCandidate:
    name: str
    score: float


@dataclass(frozen=True)
class OfficeUnderstanding:
    primary_domain: str
    candidates: tuple[DomainCandidate, ...]
    operation: str
    envelope: IntentEnvelope
    tool_requirement: ToolRequirement
    tool_hints: tuple[str, ...] = ()
    clarification_key: str = ""
    clarification_question: str = ""
    clarification_reason: str = ""

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification_key)


def _side_effect(text: str) -> SideEffectLevel:
    if _DESTRUCTIVE_RE.search(text):
        return SideEffectLevel.DESTRUCTIVE
    if _WRITE_RE.search(text):
        return SideEffectLevel.WRITE
    if _LOOKUP_RE.search(text) or _ANALYZE_RE.search(text):
        return SideEffectLevel.READ
    return SideEffectLevel.NONE


def _operation(text: str) -> str:
    if _DESTRUCTIVE_RE.search(text):
        return "delete"
    if _WRITE_RE.search(text):
        return "write"
    if _ANALYZE_RE.search(text):
        return "analyze"
    if _LOOKUP_RE.search(text):
        return "lookup"
    return "converse"


def _has_case_target(text: str) -> bool:
    if _CASE_NUMBER_RE.search(text):
        return True
    person_match = _OFFICE_PERSON_RE.search(text)
    if not person_match:
        return False
    # The permissive ``...案`` suffix can otherwise consume an action plus a
    # vague file reference (for example「移除那份檔案」→「除那份檔案」) and
    # falsely claim that a concrete party/case was supplied.
    if _VAGUE_RE.search(person_match.group(0)):
        return False
    return True


def _clarification(
    text: str,
    domains: tuple[str, ...],
    *,
    has_attachment: bool,
) -> tuple[str, str, str, tuple[MissingField, ...]]:
    domain_set = set(domains)
    explicit_case = _has_case_target(text)
    explicit_file = bool(_FILE_NAME_RE.search(text))
    vague = bool(_VAGUE_RE.search(text))

    if _MULTIPLE_TARGET_RE.search(text) and re.search(r"(?:\u4e0b\u8f09|\u4fee\u6539|\u66f4\u65b0|\u522a\u9664|\u642c\u79fb|\u540c\u6b65|\u9001\u51fa|\u6b78\u6a94)", text):
        prompt = "您提到有多個可能案件。請提供要處理那一件的本所案號、法院案號或完整當事人名稱；在唯一配對前我不會下載或修改。"
        return "case_disambiguation", prompt, "multiple_case_targets", (MissingField("case_target", prompt=prompt),)

    if "calendar" in domain_set:
        is_change = bool(re.search(r"(?:\u4fee\u6539|\u66f4\u65b0|\u6539\u5230|\u6539\u70ba|\u5ef6\u5f8c|\u63d0\u524d|\u53d6\u6d88|\u522a\u9664)", text))
        has_event_target = bool(
            explicit_case
            or _OFFICE_PERSON_RE.search(text)
            or _TIME_RANGE_RE.search(text)
            or re.search(r"[「『\"'][^\u300d』\"']{2,40}[」』\"']", text)
        )
        if is_change and not has_event_target:
            prompt = "\u60a8\u8981\u4fee\u6539\u54ea\u4e00\u7b46\u884c\u7a0b\uff1f\u8acb\u63d0\u4f9b\u6848\u865f\u3001\u7576\u4e8b\u4eba\u3001\u539f\u65e5\u671f\u6216\u884c\u7a0b\u540d\u7a31\u3002"
            return "schedule_target", prompt, "schedule_write_target_missing", (MissingField("schedule_target", prompt=prompt),)
        clock = _CLOCK_RE.search(text)
        if is_change and clock and not re.search(r"(?:\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u665a\u4e0a|\u51cc\u6668)", clock.group(0)):
            prompt = f"\u60a8\u8aaa\u7684{clock.group(1)}\u9ede\u662f\u4e0a\u5348\u9084\u662f\u4e0b\u5348\uff1f"
            return "schedule_meridiem", prompt, "schedule_time_ambiguous", (MissingField("meridiem", prompt=prompt),)

    if "legal_aid" in domain_set and re.search(r"(?:\u5831\u7d50|\u958b\u8fa6|\u56de\u5831|\u9001\u51fa)", text) and not explicit_case:
        prompt = "\u60a8\u8981\u8655\u7406\u54ea\u4e00\u4ef6\u6cd5\u6276\u6848\u4ef6\uff1f\u8acb\u63d0\u4f9b\u672c\u6240\u6848\u865f\u3001\u6cd5\u6276\u6848\u865f\u6216\u7576\u4e8b\u4eba\u3002"
        return "laf_case_target", prompt, "laf_write_target_missing", (MissingField("case_target", prompt=prompt),)

    if "drafting" in domain_set and re.search(r"(?:\u66f8\u72c0|\u8d77\u8a34\u72c0|\u7b54\u8faf\u72c0|\u8072\u8acb\u72c0|\u9673\u5831\u72c0|\u8acb\u5047\u72c0|\u8349\u64ec|\u8d77\u8349)", text) and not explicit_case and not has_attachment:
        prompt = "\u60a8\u8981\u70ba\u54ea\u4e00\u500b\u6848\u4ef6\u64b0\u5beb\u4ec0\u9ebc\u66f8\u72c0\uff1f\u8acb\u63d0\u4f9b\u672c\u6240\u6848\u865f\u6216\u7576\u4e8b\u4eba\uff0c\u4ee5\u53ca\u66f8\u72c0\u7a2e\u985e\u3002"
        return "draft_target", prompt, "draft_case_or_type_missing", (MissingField("draft_target", prompt=prompt),)

    if domain_set.intersection({"file", "summary", "translation", "transcript"}):
        file_action = bool(re.search(r"(?:\u9810\u89bd|\u4e0b\u8f09|\u958b\u555f|\u6253\u958b|\u8b80\u53d6|\u6458\u8981|\u5206\u6790|\u7ffb\u8b6f|\u7ffb\u6210|\u8f49\u9010\u5b57\u7a3f)", text))
        if file_action and (vague or re.fullmatch(r"(?:\u8acb|\u5e6b\u6211|\u9ebb\u7169)?(?:\u6458\u8981|\u7ffb\u8b6f|\u5206\u6790|\u4e0b\u8f09)(?:\u9019\u4efd)?(?:\u6a94\u6848|\u6587\u4ef6|PDF)?", re.sub(r"\s+", "", text))) and not (has_attachment or explicit_case or explicit_file):
            prompt = "\u60a8\u6307\u7684\u662f\u54ea\u4e00\u500b\u6848\u4ef6\u6216\u6a94\u6848\uff1f\u8acb\u63d0\u4f9b\u672c\u6240\u6848\u865f\u3001\u7576\u4e8b\u4eba\u3001\u6a94\u540d\uff0c\u6216\u76f4\u63a5\u4e0a\u50b3\u6a94\u6848\u3002"
            return "file_target", prompt, "content_target_unresolved", (MissingField("content_target", prompt=prompt),)

    if "legal_research" in domain_set and re.fullmatch(r"(?:\u8acb|\u5e6b\u6211|\u9ebb\u7169)?(?:\u67e5|\u627e|\u641c\u5c0b)?(?:\u5224\u6c7a|\u88c1\u5224|\u5be6\u52d9\u898b\u89e3|\u6cd5\u689d|\u6cd5\u898f)[\uff1f\u3002!\uff01]?", re.sub(r"\s+", "", text)):
        prompt = "\u60a8\u8981\u67e5\u54ea\u4e00\u500b\u6cd5\u5f8b\u722d\u9ede\u6216\u6848\u7531\uff1f\u5982\u679c\u6709\u500b\u6848\uff0c\u4e5f\u8acb\u9644\u4e0a\u95dc\u9375\u4e8b\u5be6\u3002"
        return "legal_issue", prompt, "legal_research_issue_missing", (MissingField("legal_issue", prompt=prompt),)

    if "case" in domain_set and _LOOKUP_RE.search(text) and not explicit_case:
        generic_case = re.fullmatch(r"(?:\u8acb|\u5e6b\u6211|\u9ebb\u7169)?(?:\u67e5|\u67e5\u8a62|\u67e5\u4e00\u4e0b|\u627e|\u770b\u770b|\u770b\u4e00\u4e0b)?(?:\u6848\u4ef6|\u6848\u4ef6\u9032\u5ea6|\u6848\u4ef6\u72c0\u614b)[\uff1f\u3002!\uff01]?", re.sub(r"\s+", "", text))
        if generic_case:
            prompt = "\u60a8\u8981\u67e5\u54ea\u4e00\u4ef6\u6848\u4ef6\uff1f\u8acb\u63d0\u4f9b\u672c\u6240\u6848\u865f\u3001\u6cd5\u9662\u6848\u865f\u6216\u7576\u4e8b\u4eba\u3002"
            return "case_target", prompt, "case_target_missing", (MissingField("case_target", prompt=prompt),)

    # Mutations of existing records must never infer a target from a pronoun or
    # from the current screen.  Creation and drafting are intentionally not in
    # this group: they collect their own required fields above.
    targeted_mutation = bool(re.search(r"(?:刪除|移除|上傳|搬移|移動|修改|更新|更正)", text))
    if targeted_mutation and not (explicit_case or explicit_file or has_attachment):
        prompt = "您要處理哪一個案件、檔案或既有紀錄？請提供本所案號、當事人、檔名，或直接上傳附件後再確認。"
        return "mutation_target", prompt, "mutable_target_missing", (MissingField("mutation_target", prompt=prompt),)

    if vague and _GENERIC_HANDLE_RE.search(text) and not (explicit_case or explicit_file or has_attachment):
        prompt = "您要我處理哪一個案件或哪一份資料，以及要完成什麼動作？請提供案號、當事人、檔名或直接上傳附件。"
        return "work_target", prompt, "generic_work_target_unresolved", (MissingField("work_target", prompt=prompt),)

    return "", "", "", ()


def assess_office_request(message: str, *, has_attachment: bool = False) -> OfficeUnderstanding:
    """Build a deterministic, auditable understanding of an office request."""
    text = str(message or "").strip()
    candidates = tuple(
        DomainCandidate(name, 1.0)
        for name, pattern in _DOMAIN_RULES
        if pattern.search(text)
    )
    if _CASE_NUMBER_RE.search(text) and not any(candidate.name == "case" for candidate in candidates):
        candidates = (DomainCandidate("case", 1.0),) + candidates
    domains = tuple(candidate.name for candidate in candidates)
    tool_hints = tuple(dict.fromkeys(
        hint for domain in domains if (hint := _OFFICE_TOOL_HINTS.get(domain, ""))
    ))
    primary = domains[0] if domains else "general"
    operation = _operation(text)
    side_effect = _side_effect(text)
    route = classify_intent_contract(text)
    tool_requirement = classify_tool_requirement(
        text,
        intent="QUERY" if operation in {"lookup", "analyze"} else "CMD" if side_effect.rank >= SideEffectLevel.WRITE.rank else "CHAT",
    )
    # A real office assistant never answers mutable office facts from model
    # memory. Generic policy may label an unfamiliar multi-domain query merely
    # optional; the office contract upgrades it to a verified tool lookup.
    if domains and (operation in {"lookup", "analyze"} or side_effect.rank >= SideEffectLevel.WRITE.rank):
        if tool_requirement.level != "required":
            hint = next(
                (_OFFICE_TOOL_HINTS.get(domain, "") for domain in domains if _OFFICE_TOOL_HINTS.get(domain)),
                "",
            )
            tool_requirement = ToolRequirement(
                level="required",
                tool_hint=hint,
                reason=f"office-domain facts require verified source: {primary}",
            )
    key, question, reason, missing = _clarification(text, domains, has_attachment=has_attachment)

    entities: list[Entity] = []
    for match in _CASE_NUMBER_RE.finditer(text):
        entities.append(Entity("case_identifier", match.group(0), kind="case_identifier", source="message"))
    for match in _FILE_NAME_RE.finditer(text):
        entities.append(Entity("file_name", match.group(0), kind="file_name", source="message"))
    for match in _TIME_RANGE_RE.finditer(text):
        entities.append(Entity("time_range", match.group(0), kind="time_range", source="message"))

    confidence = float(getattr(route, "confidence", 0.0) or 0.0)
    envelope = IntentEnvelope(
        intent=str(getattr(route, "kind", "unknown") or "unknown"),
        utterance=text,
        entities=tuple(entities),
        missing_fields=missing,
        confidence=max(0.0, min(1.0, confidence)),
        side_effect=side_effect,
        routing_decision={
            "domain": primary,
            "domain_candidates": list(domains),
            "operation": operation,
            "tool_level": tool_requirement.level,
            "tool_hint": tool_requirement.tool_hint,
            "tool_hints": list(tool_hints),
            "clarification_key": key,
        },
        metadata={"clarification_reason": reason},
    )
    return OfficeUnderstanding(
        primary_domain=primary,
        candidates=candidates,
        operation=operation,
        envelope=envelope,
        tool_requirement=tool_requirement,
        tool_hints=tool_hints,
        clarification_key=key,
        clarification_question=question,
        clarification_reason=reason,
    )


__all__ = ["DomainCandidate", "OfficeUnderstanding", "assess_office_request"]
