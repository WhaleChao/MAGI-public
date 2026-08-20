"""NVIDIA-assisted, source-bound judgment insight selection.

The provider never writes the quotation stored in MAGI.  It receives numbered,
deterministically extracted source spans and may return only their identifiers.
MAGI then retrieves the exact original spans, renders the summary, and applies
the normal source-support quality gate.  Provider, JSON, identifier, or quality
failure is fail-closed and cannot overwrite an existing summary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
import re
from typing import Any

from api.domains.judgment_summary_quality import (
    PracticeSpan,
    _display_source_span,
    _main_outcome,
    _statutes,
    evaluate_practice_summary,
    rank_practice_candidates,
)


_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_ALLOWED_REASON_CODES = {
    "selected",
    "no_reusable_rule",
    "issue_mismatch",
    "procedural_only",
    "fact_only",
}


@dataclass(frozen=True)
class NvidiaSummaryResult:
    success: bool
    summary: str
    error: str
    model: str
    reviewed_no_insight: bool
    selected_rule_ids: tuple[str, ...]
    selected_application_ids: tuple[str, ...]
    candidate_count: int
    quality_score: int
    response_sha256: str
    duration_ms: int
    pii_scrubbed: bool = False
    pii_counts: dict[str, int] = field(default_factory=dict)

    def audit_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("summary", None)
        return payload


def _candidate_records(
    full_text: str,
    case_reason: str,
    *,
    max_candidates: int,
) -> tuple[list[dict[str, Any]], dict[str, PracticeSpan]]:
    spans = rank_practice_candidates(
        full_text,
        case_reason,
        max_candidates=max_candidates,
    )
    records: list[dict[str, Any]] = []
    lookup: dict[str, PracticeSpan] = {}
    rule_index = application_index = other_index = 0
    for span in spans:
        if span.kind == "rule":
            rule_index += 1
            candidate_id = f"R{rule_index:02d}"
        elif span.kind == "application":
            application_index += 1
            candidate_id = f"A{application_index:02d}"
        else:
            other_index += 1
            candidate_id = f"O{other_index:02d}"
        lookup[candidate_id] = span
        records.append(
            {
                "id": candidate_id,
                "kind": span.kind,
                "score": span.score,
                "issue_terms": list(span.relevant_terms[:6]),
                "text": span.text,
            }
        )
    return records, lookup


def _selection_prompt(case_reason: str, records: list[dict[str, Any]]) -> str:
    schema = {
        "usable": True,
        "rule_ids": ["R01"],
        "application_ids": ["A01"],
        "confidence": 0.0,
        "reason_code": "selected",
    }
    return (
        "你是臺灣裁判實務見解的選段審查員。你不能摘要、改寫、補字或輸出候選原文；"
        "只能從候選段落中選擇編號。\n"
        f"案件議題：{case_reason or '未分類'}\n"
        "選擇標準：\n"
        "1. rule_ids 必須是可移用到其他案件的法律規則、構成要件、舉證責任或權威見解。\n"
        "2. application_ids 是法院把規則適用到本案的理由；若候選中有 A 編號，"
        "且所選 R 只是法條文字、沒有構成要件或權威見解，必須選 1 個與議題最相符的 A 編號。"
        "只有所選 R 本身已有完整涵攝或候選中沒有 A 編號時，才可不選。\n"
        "3. 實務見解不能只有過短法條片段；若所選 R 的中文合計未滿 40 字，"
        "且候選中另有延續同一規則的 R 編號，必須連同該 R 一併選取。\n"
        "4. 當事人主張、純事實、主文、制式程序、單純抄法條均不可選。\n"
        "5. 最多選 2 個 R 編號、1 個 A 編號。若沒有可用規則，usable=false 且兩清單為空。\n"
        "6. 只輸出一個 JSON 物件，不得有 markdown 或說明文字。\n"
        "reason_code 僅可為 selected、no_reusable_rule、issue_mismatch、procedural_only、fact_only。\n"
        f"JSON 格式：{json.dumps(schema, ensure_ascii=False)}\n"
        "候選段落：\n"
        f"{json.dumps(records, ensure_ascii=False, separators=(',', ':'))}"
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    fenced = _JSON_FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("selector_response_not_json")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("selector_response_not_json") from exc
    if not isinstance(value, dict):
        raise ValueError("selector_response_not_object")
    return value


def _validate_selection(
    payload: dict[str, Any],
    lookup: dict[str, PracticeSpan],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], str]:
    if type(payload.get("usable")) is not bool:
        raise ValueError("selector_usable_not_boolean")
    reason_code = str(payload.get("reason_code") or "").strip()
    if reason_code not in _ALLOWED_REASON_CODES:
        raise ValueError("selector_reason_code_invalid")
    raw_rules = payload.get("rule_ids")
    raw_applications = payload.get("application_ids")
    if not isinstance(raw_rules, list) or not isinstance(raw_applications, list):
        raise ValueError("selector_ids_not_lists")
    rule_ids = tuple(str(value).strip() for value in raw_rules)
    application_ids = tuple(str(value).strip() for value in raw_applications)
    if len(rule_ids) > 2 or len(application_ids) > 1:
        raise ValueError("selector_too_many_ids")
    all_ids = (*rule_ids, *application_ids)
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("selector_duplicate_id")
    if any(candidate_id not in lookup for candidate_id in all_ids):
        raise ValueError("selector_unknown_id")
    if not payload["usable"]:
        if all_ids:
            raise ValueError("selector_unusable_with_ids")
        return False, (), (), reason_code
    if not rule_ids:
        raise ValueError("selector_usable_without_rule")
    if any(
        lookup[candidate_id].kind != "rule"
        or lookup[candidate_id].score < 26
        for candidate_id in rule_ids
    ):
        raise ValueError("selector_invalid_rule")
    if any(
        lookup[candidate_id].kind != "application"
        or lookup[candidate_id].score < 18
        for candidate_id in application_ids
    ):
        raise ValueError("selector_invalid_application")
    return True, rule_ids, application_ids, reason_code


def _render_summary(
    *,
    full_text: str,
    case_reason: str,
    lookup: dict[str, PracticeSpan],
    rule_ids: tuple[str, ...],
    application_ids: tuple[str, ...],
    model: str,
    max_chars: int,
) -> str:
    rules = [lookup[candidate_id] for candidate_id in rule_ids]
    applications = [lookup[candidate_id] for candidate_id in application_ids]
    matched_terms = tuple(
        dict.fromkeys(
            term
            for span in (*rules, *applications)
            for term in span.relevant_terms
        )
    )
    lines = [
        "## 法律爭點",
        f"- {case_reason or '未分類'}"
        + (f"；命中：{'、'.join(matched_terms[:6])}" if matched_terms else ""),
        "",
        "## 實務見解",
    ]
    lines.extend(f"- {_display_source_span(span.text)}" for span in rules)
    if applications:
        lines.extend(["", "## 法院涵攝"])
        lines.extend(
            f"- {_display_source_span(span.text)}"
            for span in applications
        )
    outcome = _main_outcome(full_text)
    if outcome:
        lines.extend(["", "## 裁判結果", f"- {outcome}"])
    statutes = _statutes(
        "\n".join(span.text for span in (*rules, *applications))
    )
    if statutes:
        lines.extend(["", "## 適用法條", "、".join(statutes[:12])])
    lines.extend(
        [
            "",
            "## 摘要方式",
            f"NVIDIA {model or 'NIM'} 選段；內容均為裁判原文（僅正規化空白）",
        ]
    )
    summary = "\n".join(lines).strip()
    return summary if len(summary) <= max_chars else ""


def _source_bound_application_rescue(
    *,
    lookup: dict[str, PracticeSpan],
    rule_ids: tuple[str, ...],
    application_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Add one deterministic application span when the selector omitted it.

    This is deliberately narrower than a model retry.  The candidate list has
    already passed the deterministic issue and score filters, and the selected
    text is still copied from the judgment verbatim.  We only rescue a missing
    application; an explicit but invalid provider id continues to fail closed.
    """
    if application_ids:
        return application_ids
    selected_terms = {
        term
        for candidate_id in rule_ids
        for term in lookup[candidate_id].relevant_terms
    }
    candidates = [
        (candidate_id, span)
        for candidate_id, span in lookup.items()
        if span.kind == "application" and span.score >= 18
    ]
    if selected_terms:
        overlapping = [
            item
            for item in candidates
            if selected_terms.intersection(item[1].relevant_terms)
        ]
        if not overlapping:
            return ()
        candidates = overlapping
    if not candidates:
        return ()
    candidates.sort(
        key=lambda item: (
            item[1].score,
            len(item[1].relevant_terms),
            -len(item[1].text),
            item[0],
        ),
        reverse=True,
    )
    return (candidates[0][0],)


def _can_pass_without_application(
    *,
    full_text: str,
    case_reason: str,
    lookup: dict[str, PracticeSpan],
    max_chars: int,
) -> bool:
    """Prove whether one or two rule spans can pass without an application.

    The hosted selector may choose at most two rule identifiers.  When the
    deterministic candidate set contains no application paragraph and every
    permitted rule combination fails the same local quality gate, an external
    request cannot produce a storable result.  This preflight prevents a
    doomed request while preserving high-authority/doctrinal rule-only cases.
    """
    rule_ids = tuple(
        candidate_id
        for candidate_id, span in lookup.items()
        if span.kind == "rule" and span.score >= 26
    )
    selections = [(candidate_id,) for candidate_id in rule_ids]
    selections.extend(
        (rule_ids[left], rule_ids[right])
        for left in range(len(rule_ids))
        for right in range(left + 1, len(rule_ids))
    )
    for selected in selections:
        probe = _render_summary(
            full_text=full_text,
            case_reason=case_reason,
            lookup=lookup,
            rule_ids=selected,
            application_ids=(),
            model="local-preflight",
            max_chars=max_chars,
        )
        if probe and evaluate_practice_summary(probe, full_text, case_reason).ok:
            return True
    return False


def summarize_with_nvidia(
    full_text: str,
    case_reason: str = "",
    *,
    timeout_sec: int = 240,
    model: str | None = None,
    max_candidates: int = 14,
    max_chars: int = 1800,
) -> NvidiaSummaryResult:
    """Select and render a high-value source-bound summary via NVIDIA NIM."""
    records, lookup = _candidate_records(
        full_text,
        case_reason,
        max_candidates=max_candidates,
    )
    if not any(record["kind"] == "rule" for record in records):
        return NvidiaSummaryResult(
            False, "", "no_deterministic_rule_candidates", "", True,
            (), (), len(records), 0, "", 0,
        )
    if (
        not any(span.kind == "application" for span in lookup.values())
        and not _can_pass_without_application(
            full_text=full_text,
            case_reason=case_reason,
            lookup=lookup,
            max_chars=max_chars,
        )
    ):
        return NvidiaSummaryResult(
            False, "", "reviewed:no_source_application", "", True,
            (), (), len(records), 0, "", 0,
        )

    from skills.bridge.nim_heavy import background_heavy_authorization, run_nim_chat

    provider = run_nim_chat(
        prompt=_selection_prompt(case_reason, records),
        timeout_sec=max(60, int(timeout_sec)),
        task_type="judgment_summary",
        require_pii_scrub=True,
        data_classification="public_judgment",
        privacy_profile="public_judgment",
        restore_pii=False,
        system_prompt=(
            "Follow the supplied JSON schema exactly. Select identifiers only; "
            "never reproduce or rewrite source text."
        ),
        heavy=True,
        model=model,
        # Cron injects the immutable job id; without it the hosted request
        # fails closed rather than borrowing any conversational authorization.
        background_heavy_authorized=background_heavy_authorization(
            os.environ.get("MAGI_CRON_JOB_ID", "")
        ),
    )
    response = str(provider.get("response") or "")
    response_sha256 = (
        hashlib.sha256(response.encode("utf-8")).hexdigest()
        if response
        else ""
    )
    common = {
        "model": str(provider.get("model") or model or ""),
        "candidate_count": len(records),
        "response_sha256": response_sha256,
        "duration_ms": int(provider.get("duration_ms") or 0),
        "pii_scrubbed": bool(provider.get("pii_scrubbed")),
        "pii_counts": {
            str(key): int(value or 0)
            for key, value in dict(provider.get("pii_counts") or {}).items()
        },
    }
    if not provider.get("success"):
        return NvidiaSummaryResult(
            False, "", f"provider:{provider.get('error') or 'unknown'}",
            common["model"], False, (), (), common["candidate_count"], 0,
            common["response_sha256"], common["duration_ms"],
            pii_scrubbed=common["pii_scrubbed"],
            pii_counts=common["pii_counts"],
        )
    try:
        selection = _parse_json_object(response)
        usable, rule_ids, application_ids, reason_code = _validate_selection(
            selection,
            lookup,
        )
    except ValueError as exc:
        return NvidiaSummaryResult(
            False, "", str(exc), common["model"], False, (), (),
            common["candidate_count"], 0, common["response_sha256"],
            common["duration_ms"],
            pii_scrubbed=common["pii_scrubbed"],
            pii_counts=common["pii_counts"],
        )
    if not usable:
        return NvidiaSummaryResult(
            False, "", f"reviewed:{reason_code}", common["model"], True,
            (), (), common["candidate_count"], 0, common["response_sha256"],
            common["duration_ms"],
            pii_scrubbed=common["pii_scrubbed"],
            pii_counts=common["pii_counts"],
        )
    summary = _render_summary(
        full_text=full_text,
        case_reason=case_reason,
        lookup=lookup,
        rule_ids=rule_ids,
        application_ids=application_ids,
        model=common["model"],
        max_chars=max_chars,
    )
    if not summary:
        return NvidiaSummaryResult(
            False, "", "rendered_summary_too_long", common["model"], False,
            rule_ids, application_ids, common["candidate_count"], 0,
            common["response_sha256"], common["duration_ms"],
            pii_scrubbed=common["pii_scrubbed"],
            pii_counts=common["pii_counts"],
        )
    quality = evaluate_practice_summary(summary, full_text, case_reason)
    if quality.reason == "statute_only_without_application" and not application_ids:
        rescued_application_ids = _source_bound_application_rescue(
            lookup=lookup,
            rule_ids=rule_ids,
            application_ids=application_ids,
        )
        if rescued_application_ids:
            rescued_summary = _render_summary(
                full_text=full_text,
                case_reason=case_reason,
                lookup=lookup,
                rule_ids=rule_ids,
                application_ids=rescued_application_ids,
                model=common["model"],
                max_chars=max_chars,
            )
            if rescued_summary:
                rescued_quality = evaluate_practice_summary(
                    rescued_summary,
                    full_text,
                    case_reason,
                )
                if rescued_quality.ok:
                    summary = rescued_summary
                    quality = rescued_quality
                    application_ids = rescued_application_ids
        if (
            not quality.ok
            and quality.reason == "statute_only_without_application"
            and not any(span.kind == "application" for span in lookup.values())
        ):
            # The deterministic candidate space proves that this judgment has
            # no source-bound application paragraph that could make the bare
            # rule practice-ready.  Retrying the same public source through a
            # larger model cannot create missing judicial reasoning.  Record a
            # terminal reviewed-no-insight outcome instead of storing a
            # statute-only digest or repeatedly charging the external budget.
            return NvidiaSummaryResult(
                False, "", "reviewed:no_source_application",
                common["model"], True, rule_ids, (),
                common["candidate_count"], quality.score,
                common["response_sha256"], common["duration_ms"],
                pii_scrubbed=common["pii_scrubbed"],
                pii_counts=common["pii_counts"],
            )
    if not quality.ok:
        return NvidiaSummaryResult(
            False, "", f"quality:{quality.reason}", common["model"], False,
            rule_ids, application_ids, common["candidate_count"],
            quality.score, common["response_sha256"], common["duration_ms"],
            pii_scrubbed=common["pii_scrubbed"],
            pii_counts=common["pii_counts"],
        )
    return NvidiaSummaryResult(
        True, summary, "", common["model"], False, rule_ids,
        application_ids, common["candidate_count"], quality.score,
        common["response_sha256"], common["duration_ms"],
        pii_scrubbed=common["pii_scrubbed"],
        pii_counts=common["pii_counts"],
    )
