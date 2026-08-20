"""Evidence-first quality rules for Taiwan judgment research.

The functions in this module are deliberately deterministic.  Retrieval
providers may discover candidates, but only a locally verified official copy
is eligible to support a pleading citation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


VERIFIED_LOCAL = "verified_local_official"
LOCAL_REVIEWED = "reviewed_local_insight"
EXTERNAL_CANDIDATE = "external_candidate"
UNVERIFIED = "unverified"

_CASE_NO_RE = re.compile(
    r"(?:最高法院|最高行政法院|憲法法庭|臺灣[\u4e00-\u9fff]{1,8}法院)?\s*"
    r"\d{2,3}\s*年?度\s*[\u4e00-\u9fff]{1,12}\s*字\s*第?\s*\d+\s*號"
)
_INTERNAL_CASE_RE = re.compile(r"\b20\d{2}-\d{4}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_TW_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?886[- ]?)?(?:0?9\d{8}|0\d{1,2}[- ]?\d{6,8})(?!\d)")
_ADDRESS_RE = re.compile(
    r"(?:住址|住所|居所|地址|戶籍地)\s*[:：]?\s*"
    r"[\u4e00-\u9fff]{2,20}(?:市|縣)[\u4e00-\u9fff0-9\-弄巷號樓之]{3,40}"
)
_LABELLED_NAME_RE = re.compile(
    r"(?:當事人|原告|被告|聲請人|相對人|告訴人|被害人|委任人|客戶|法扶申請人)"
    r"\s*[:：]?\s*([\u4e00-\u9fff]{2,4})"
)
# A legal-research query is not a safe place to infer that an unlabelled name
# is public.  The common three-character Taiwanese-name shape catches the
# ordinary case where a chat user writes ``王小明`` without saying
# ``當事人：``.  Public judicial actors remain usable only when explicitly
# labelled as such below.
_COMMON_TW_SURNAME_RE = re.compile(
    r"[王李張劉陳楊黃趙周吳徐朱孫胡郭何高林羅鄭梁謝宋唐許韓馮鄧曹彭曾蕭田董潘袁蔡蔣余杜葉程蘇魏呂丁任沈姚盧傅鍾姜崔譚廖范汪金石熊陸郝孔白崔康毛邱秦江史顧侯邵孟龍萬段雷錢湯尹黎易常武喬賀賴龔文施洪顏倪嚴牛溫芮藍彭游詹簡童方戴夏鍾]"
    r"[\u4e00-\u9fff]{1,2}"
)
_PUBLIC_JUDICIAL_ACTOR_RE = re.compile(r"(?:審判長|受命法官|陪席法官|法官)\s*[:：]?\s*$")
_PRIVATE_NARRATIVE_RE = re.compile(
    r"(?:我方|本所|客戶|當事人|委任人|附件|卷證|病歷|帳戶|薪資|對話紀錄|"
    r"聯絡方式|住址|身分證|電話|電子郵件|出生日期)"
)
_SENSITIVE_NARRATIVE_RE = re.compile(
    r"(?:我方|本所|客戶|委任人|附件|卷證|病歷|帳戶|薪資|對話紀錄|"
    r"聯絡方式|住址|身分證|電話|電子郵件|出生日期)"
)
_LEGAL_SIGNAL_RE = re.compile(
    r"(?:民法|刑法|行政法|公司法|證券交易法|勞動基準法|消費者債務清理條例|"
    r"民事訴訟法|刑事訴訟法|行政訴訟法|構成要件|舉證責任|因果關係|"
    r"違法性|過失|故意|契約|損害賠償|更生|清算|詐欺|傷害|判決|裁判|法條|"
    r"最高法院|大法庭|憲法法庭)"
)
_REASONING_MARKERS = (
    "本院認為",
    "本院判斷",
    "本院審酌",
    "法院認為",
    "應解為",
    "按",
    "惟",
    "準此",
    "是以",
    "足認",
    "應認",
    "堪認",
    "不得",
    "有理由",
    "無理由",
)
_PARTY_MARKERS = (
    "原告主張",
    "被告辯稱",
    "聲請人主張",
    "相對人辯稱",
    "告訴人指稱",
    "上訴人主張",
)


@dataclass(frozen=True)
class PrivacyDecision:
    safe_query: str
    external_allowed: bool
    redactions: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "safe_query": self.safe_query,
            "external_allowed": self.external_allowed,
            "redactions": list(self.redactions),
            "reasons": list(self.reasons),
        }


def prepare_external_legal_query(raw_query: str, *, max_chars: int = 160) -> PrivacyDecision:
    """Create the only text permitted to leave MAGI for legal retrieval.

    Court docket numbers are public identifiers and remain searchable.  Office
    matter numbers and personal identifiers are removed.  A private narrative
    without a recognizable legal issue fails closed instead of being sent.
    """

    text = str(raw_query or "").strip()
    text = re.sub(r"^@MAGI\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(
        r"^(?:實務見解|法律見解|法院見解|查判決|找判決|判決搜尋|搜尋判決|查裁判|找裁判)\s*",
        "",
        text,
    ).strip()
    redactions: list[str] = []

    def _redact(pattern: re.Pattern[str], label: str, value: str) -> str:
        if pattern.search(value):
            redactions.append(label)
            return pattern.sub(f"〔{label}已移除〕", value)
        return value

    text = _redact(_EMAIL_RE, "電子郵件", text)
    text = _redact(_TW_ID_RE, "身分證字號", text)
    text = _redact(_PHONE_RE, "電話", text)
    text = _redact(_ADDRESS_RE, "地址", text)
    text = _redact(_INTERNAL_CASE_RE, "內部案號", text)
    if _LABELLED_NAME_RE.search(text):
        redactions.append("姓名")
        text = _LABELLED_NAME_RE.sub(lambda m: m.group(0).replace(m.group(1), "某當事人"), text)

    # Label-free names are personal data by default.  Do not guess that they
    # are judges merely because the query also contains a legal term.  A judge
    # name remains searchable when the user explicitly marks the public role.
    public_judge_context = bool(
        ("法院" in text or "法庭" in text)
        and re.search(r"(?:量刑|判決趨勢|裁判理由|裁判|判決)", text)
    )
    unlabelled_name_found = False
    chunks: list[str] = []
    cursor = 0
    for match in _COMMON_TW_SURNAME_RE.finditer(text):
        prefix = text[max(0, match.start() - 12) : match.start()]
        candidate = match.group(0)
        # Do not mistake wording such as ``地方法院`` for a personal name.
        if candidate.endswith("法院") or (prefix.endswith("主") and candidate.startswith("張")):
            continue
        if _PUBLIC_JUDICIAL_ACTOR_RE.search(prefix) or public_judge_context:
            continue
        chunks.append(text[cursor : match.start()])
        chunks.append("某當事人")
        cursor = match.end()
        unlabelled_name_found = True
    if unlabelled_name_found:
        chunks.append(text[cursor:])
        text = "".join(chunks)
        redactions.append("未標籤姓名")

    # Remove redaction placeholders entirely; the remote provider does not need
    # to learn even which personal fields were present.
    text = re.sub(r"〔[^〕]+已移除〕", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:，,。；;")
    private_narrative = bool(_PRIVATE_NARRATIVE_RE.search(text))
    sensitive_narrative = bool(_SENSITIVE_NARRATIVE_RE.search(text))
    court_docket = bool(_CASE_NO_RE.search(text))
    legal_signal = bool(_LEGAL_SIGNAL_RE.search(text))
    reasons: list[str] = []
    external_allowed = bool(text)
    if sensitive_narrative:
        external_allowed = False
        reasons.append("private_narrative_requires_local_abstraction")
    elif private_narrative and not (court_docket or legal_signal):
        external_allowed = False
        reasons.append("private_narrative_without_abstract_legal_issue")
    if unlabelled_name_found:
        external_allowed = False
        reasons.append("unlabelled_person_name_requires_local_abstraction")
    if len(text) < 2:
        external_allowed = False
        reasons.append("empty_after_redaction")
    if len(redactions) >= 2 and not (court_docket or legal_signal):
        external_allowed = False
        reasons.append("too_many_private_identifiers")

    return PrivacyDecision(
        safe_query=text[: max(20, int(max_chars))],
        external_allowed=external_allowed,
        redactions=tuple(dict.fromkeys(redactions)),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def canonical_case_key(value: Any) -> str:
    text = str(value or "").replace("台", "臺")
    text = re.sub(r"\s+", "", text)
    match = re.search(r"(\d{2,3})年?度([\u4e00-\u9fff]{1,12})字第?(\d+)號", text)
    if match:
        return f"{int(match.group(1))}:{match.group(2)}:{int(match.group(3))}"
    # TLR doc IDs commonly preserve comma-delimited JYEAR/JCASE/JNO.
    parts = [part.strip() for part in text.split(",")]
    if len(parts) >= 4 and parts[1].isdigit() and parts[3].isdigit():
        return f"{int(parts[1])}:{parts[2]}:{int(parts[3])}"
    return ""


def verification_state(item: dict[str, Any]) -> str:
    explicit = str(item.get("verification_state") or "").strip()
    if explicit:
        return explicit
    source = str(item.get("source") or item.get("source_type") or "").lower()
    if source in {"court_judgments_local", "court_judgments", "judicial_api_official"}:
        return VERIFIED_LOCAL
    if source == "legal_insights" and item.get("human_reviewed"):
        return LOCAL_REVIEWED
    if "tw_legal_rag" in source or "tlr" in source:
        return EXTERNAL_CANDIDATE
    return UNVERIFIED


def is_draft_eligible(item: dict[str, Any]) -> bool:
    if item.get("draft_eligible") is False:
        return False
    state = verification_state(item)
    if state == LOCAL_REVIEWED:
        return True
    if state != VERIFIED_LOCAL:
        return False

    # An official local copy proves provenance, not usefulness.  Court
    # summaries used in pleadings must also pass the stricter practice-ready
    # gate (source support, score and either case application or a narrow
    # high-court doctrinal exception).
    summary = str(item.get("summary") or item.get("summary_full") or item.get("insight_text") or "").strip()
    source_text = str(item.get("full_text") or item.get("source_text") or "").strip()
    if summary and source_text and "## 實務見解" in summary:
        from api.domains.judgment_summary_quality import evaluate_practice_ready_summary

        quality = evaluate_practice_ready_summary(
            summary,
            source_text,
            str(item.get("case_reason") or item.get("reason") or ""),
            str(item.get("court") or item.get("court_name") or ""),
        )
        return bool(quality.ok)

    # Research cards can be built directly from an official full text before
    # a structured summary exists.  In that path, only a source-exact court
    # application span is citeable; a bare rule or metadata-only row is not.
    for span in item.get("support_spans") or []:
        if not isinstance(span, dict) or span.get("source_exact") is not True:
            continue
        span_text = str(span.get("text") or "").strip()
        if source_text and span_text not in source_text:
            continue
        if re.search(r"本院認為|本院判斷|本院審酌|法院認為|經查|足認|應認|堪認|尚難", span_text):
            return True
    return False


def _court_authority_score(item: dict[str, Any]) -> tuple[int, list[str]]:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("court_name", "court", "citation_text", "title", "case_category")
    )
    score = 30
    reasons: list[str] = []
    if "憲法法庭" in text or "憲判" in text:
        score, reasons = 100, ["憲法法庭"]
    elif "大法庭" in text:
        score, reasons = 98, ["大法庭統一見解"]
    elif "最高法院" in text or "最高行政法院" in text:
        score, reasons = 92, ["最高審級"]
    elif "高等行政法院" in text or "高等法院" in text:
        score, reasons = 72, ["高等審級"]
    elif "地方法院" in text or "簡易庭" in text:
        score, reasons = 48, ["第一審實務"]
    if re.search(r"程序|裁判費|補正|移送管轄|支付命令", text):
        score -= 20
        reasons.append("程序性裁判降權")
    history = item.get("case_history") or []
    history_text = " ".join(str(part) for part in history)
    if re.search(r"廢棄|撤銷|發回", history_text):
        score -= 35
        reasons.append("審級歷程含廢棄／撤銷")
    return max(0, min(100, score)), reasons


def _query_terms(query: str) -> list[str]:
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(query or ""))
    terms: list[str] = []
    for token in compact.split():
        if len(token) < 2 or token in {"實務見解", "法律見解", "判決", "裁判", "法院"}:
            continue
        terms.append(token)
        if re.search(r"[\u4e00-\u9fff]", token) and len(token) >= 4:
            terms.extend(token[i : i + 2] for i in range(len(token) - 1))
    return list(dict.fromkeys(terms))[:20]


def factual_similarity_score(query: str, item: dict[str, Any]) -> tuple[int, list[str]]:
    terms = _query_terms(query)
    haystack = re.sub(
        r"\s+",
        "",
        " ".join(
            str(item.get(key) or "")
            for key in (
                "citation_text",
                "title",
                "case_category",
                "summary_full",
                "summary_preview",
                "full_text_excerpt",
                "full_text",
            )
        ),
    )
    if not terms or not haystack:
        return 0, ["缺少可比較文字"]
    weighted_hits = sum((2 if len(term) >= 3 else 1) for term in terms if term in haystack)
    total = sum(2 if len(term) >= 3 else 1 for term in terms)
    ratio = weighted_hits / max(1, total)
    score = int(round(100 * math.sqrt(ratio)))
    matched = [term for term in terms if term in haystack][:5]
    reasons = ["命中：" + "、".join(matched)] if matched else ["未命中核心爭點"]
    return max(0, min(100, score)), reasons


def _split_reasoning_units(text: str) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    parts = re.split(r"\n{2,}|(?<=。)\s*(?=(?:本院|法院|按|惟|準此|是以|足認|應認))", cleaned)
    return [re.sub(r"\s+", " ", part).strip() for part in parts if len(part.strip()) >= 35]


def supporting_spans(query: str, item: dict[str, Any], *, limit: int = 3) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    text = str(
        item.get("full_text")
        or item.get("full_text_excerpt")
        or item.get("summary_full")
        or item.get("summary_preview")
        or ""
    )
    ranked: list[tuple[int, int, str]] = []
    offset = 0
    for unit in _split_reasoning_units(text):
        start = text.find(unit, offset)
        if start < 0:
            start = text.find(unit)
        offset = max(offset, start + len(unit)) if start >= 0 else offset
        compact = re.sub(r"\s+", "", unit)
        score = sum(18 for term in terms if term and term in compact)
        score += sum(12 for marker in _REASONING_MARKERS if marker in compact)
        score -= sum(30 for marker in _PARTY_MARKERS if marker in compact[:80])
        if re.search(r"第\s*\d+\s*條", compact):
            score += 14
        if score > 0:
            ranked.append((score, max(0, start), unit))
    ranked.sort(key=lambda row: (row[0], len(row[2])), reverse=True)
    out: list[dict[str, Any]] = []
    for score, start, unit in ranked[: max(1, int(limit))]:
        excerpt = unit[:900].rstrip()
        out.append(
            {
                "text": excerpt,
                "start": start,
                "end": start + len(excerpt),
                "score": score,
                "source_exact": bool(text and excerpt in text),
            }
        )
    return out


def detect_outcome(item: dict[str, Any]) -> str:
    text = str(item.get("full_text") or item.get("full_text_excerpt") or "")
    head = text[:1800]
    for pattern, label in (
        (r"上訴駁回|抗告駁回|原告之訴駁回|聲請駁回", "駁回"),
        (r"原判決廢棄|原裁定廢棄", "廢棄原裁判"),
        (r"撤銷", "撤銷"),
        (r"准予|應給付|有罪", "准許／有利認定"),
        (r"無罪", "無罪"),
    ):
        if re.search(pattern, head):
            return label
    return "結果需核對主文"


def enrich_and_rank_items(query: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        authority, authority_reasons = _court_authority_score(item)
        similarity, similarity_reasons = factual_similarity_score(query, item)
        state = verification_state(item)
        spans = supporting_spans(query, item)
        item.update(
            {
                "verification_state": state,
                "draft_eligible": is_draft_eligible(
                    {**item, "verification_state": state, "support_spans": spans}
                ),
                "authority_score": authority,
                "similarity_score": similarity,
                "authority_reasons": authority_reasons,
                "similarity_reasons": similarity_reasons,
                "support_spans": spans,
                "outcome": detect_outcome(item),
            }
        )
        # Discovery ordering favours fit, but verified sources and higher courts
        # get deterministic tie-breaking credit.
        verified_bonus = 15 if state == VERIFIED_LOCAL else (8 if state == LOCAL_REVIEWED else 0)
        item["combined_rank_score"] = round(0.55 * similarity + 0.45 * authority + verified_bonus, 2)
        enriched.append(item)
    enriched.sort(
        key=lambda row: (
            bool(row.get("draft_eligible")),
            float(row.get("combined_rank_score") or 0),
            float(row.get("authority_score") or 0),
        ),
        reverse=True,
    )
    for index, item in enumerate(enriched, start=1):
        item["research_rank"] = index
        item["citation_id"] = str(item.get("citation_id") or f"J{index}")
    return enriched


def build_practice_view_card(query: str, item: dict[str, Any]) -> dict[str, Any]:
    spans = item.get("support_spans") or supporting_spans(query, item)
    rule_span = next(
        (
            span
            for span in spans
            if re.search(r"第\s*\d+\s*條|應解為|構成要件|舉證責任", str(span.get("text") or ""))
        ),
        spans[0] if spans else None,
    )
    application_span = next(
        (
            span
            for span in spans
            if re.search(r"本院認為|本院判斷|足認|應認|堪認", str(span.get("text") or ""))
        ),
        spans[1] if len(spans) > 1 else rule_span,
    )
    limitation_span = next(
        (
            span
            for span in spans
            if re.search(r"惟|但|除非|例外|尚難", str(span.get("text") or ""))
        ),
        None,
    )
    return {
        "schema": "magi.practice-view-card/v2",
        "citation_id": item.get("citation_id"),
        "citation_text": item.get("citation_text") or item.get("title") or "",
        "source_url": item.get("url") or item.get("citation_url") or "",
        "court": item.get("court_name") or item.get("court") or "",
        "date": item.get("judgment_date") or item.get("jdate") or "",
        "issue": query,
        "rule": str((rule_span or {}).get("text") or ""),
        "application": str((application_span or {}).get("text") or ""),
        "outcome": item.get("outcome") or detect_outcome(item),
        "limitation": str((limitation_span or {}).get("text") or ""),
        "authority_score": item.get("authority_score"),
        "similarity_score": item.get("similarity_score"),
        "verification_state": verification_state(item),
        "draft_eligible": is_draft_eligible(item),
        "support_spans": spans,
        "case_history": item.get("case_history") or [],
    }


def grounded_summary_evidence(full_text: str, *, query: str = "") -> dict[str, Any]:
    """Build a source-mapped fallback summary without asking a language model."""
    item = {"full_text": str(full_text or "")}
    spans = supporting_spans(query, item, limit=6)
    evidence = [
        {
            **span,
            "span_id": f"S{index}",
        }
        for index, span in enumerate(spans, start=1)
    ]
    rules = [
        span
        for span in evidence
        if re.search(r"第\s*\d+\s*條|應解為|構成要件|舉證責任|因果關係", str(span.get("text") or ""))
    ]
    applications = [
        span
        for span in evidence
        if re.search(r"本院認為|本院判斷|足認|應認|堪認|尚難", str(span.get("text") or ""))
    ]
    outcome = detect_outcome(item)
    lines = [
        "1) 法律爭點",
        f"- {query or '應依完整案情與下列法院理由確認具體爭點。'}",
        "",
        "2) 法律規則與法院見解",
    ]
    for span in (rules or evidence)[:3]:
        lines.append(f"- [{span['span_id']}] {span['text']}")
    lines.extend(["", "3) 法院涵攝"])
    for span in (applications or evidence[1:])[:2]:
        lines.append(f"- [{span['span_id']}] {span['text']}")
    lines.extend(
        [
            "",
            "4) 結果方向",
            f"- {outcome}",
            "",
            "5) 核對狀態",
            "- 每一要旨均綁定上列原文段落；正式引用仍須律師核對裁判全文與審級歷程。",
        ]
    )
    return {
        "ok": bool(evidence),
        "summary": "\n".join(lines),
        "evidence": evidence,
        "outcome": outcome,
    }


def validate_span_citations(text: str, evidence: Iterable[dict[str, Any]]) -> dict[str, Any]:
    allowed = {str(span.get("span_id") or "") for span in evidence if str(span.get("span_id") or "")}
    found = set(re.findall(r"\[(S\d+)\]", str(text or "")))
    unknown = sorted(found - allowed)
    return {
        "ok": bool(found) and not unknown,
        "found": sorted(found),
        "unknown": unknown,
        "allowed": sorted(allowed),
    }


def _exact_support_spans(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only source-verifiable spans suitable for a citation lock.

    A selected insight can originate from a persisted/manual record, so merely
    having a non-empty ``support_spans`` list is not proof that the quoted
    proposition remains traceable to its underlying text.  When local source
    text is available, also re-check the span against it; otherwise require
    the producer's explicit exact-source attestation.  Ambiguity is rejected
    from the automatic citation bundle and remains available for review.
    """
    source_text = str(
        item.get("full_text")
        or item.get("full_text_excerpt")
        or item.get("summary_full")
        or item.get("summary_preview")
        or ""
    )
    verified: list[dict[str, Any]] = []
    for raw_span in item.get("support_spans") or []:
        if not isinstance(raw_span, dict):
            continue
        span_text = str(raw_span.get("text") or "").strip()
        if not span_text or raw_span.get("source_exact") is not True:
            continue
        if source_text and span_text not in source_text:
            continue
        verified.append(raw_span)
    return verified


def citation_lock_for_items(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    allowed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in items:
        citation = str(item.get("citation_text") or item.get("title") or item.get("case_number") or "").strip()
        exact_spans = _exact_support_spans(item)
        record = {
            "citation_id": item.get("citation_id") or "",
            "citation_text": citation,
            "case_key": canonical_case_key(citation) or canonical_case_key(item.get("doc_id") or item.get("jid")),
            "verification_state": verification_state(item),
            "support_spans": exact_spans,
        }
        if is_draft_eligible(item) and record["case_key"] and exact_spans:
            allowed.append(record)
        else:
            if not exact_spans:
                record["rejection_reason"] = "missing_exact_source_support"
            elif not record["case_key"]:
                record["rejection_reason"] = "missing_canonical_case_key"
            else:
                record["rejection_reason"] = "verification_state_not_draft_eligible"
            rejected.append(record)
    return {
        "schema": "magi.citation-lock/v1",
        "allowed": allowed,
        "rejected": rejected,
        "allowed_case_keys": [row["case_key"] for row in allowed],
        "human_approval_required": True,
    }


def validate_text_against_citation_lock(text: str, lock: dict[str, Any]) -> dict[str, Any]:
    allowed = set(str(key) for key in (lock.get("allowed_case_keys") or []) if str(key))
    found: list[dict[str, Any]] = []
    for match in _CASE_NO_RE.finditer(str(text or "")):
        citation = match.group(0).strip()
        key = canonical_case_key(citation)
        found.append({"citation_text": citation, "case_key": key, "allowed": bool(key and key in allowed)})
    violations = [row for row in found if not row["allowed"]]
    return {
        "ok": not violations,
        "citations_found": len(found),
        "allowed_count": len(found) - len(violations),
        "violations": violations,
        "human_approval_required": True,
    }
