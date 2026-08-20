"""
api.osc.judicial -- Judicial / legal-data functions extracted from server.py.

Functions:
    _osc_fetch_fulltext_from_exact_case_search
    _osc_pick_best_manifest_item
    _osc_summarize_legal_insight
    _osc_fetch_fulltext_from_judicial
    _osc_collect_insights
    _osc_doc_kind_match
    _osc_doc_kind_label
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re

from api.legal_research_quality import EXTERNAL_CANDIDATE, LOCAL_REVIEWED, VERIFIED_LOCAL
from api.domains.judgment_summary_quality import evaluate_practice_ready_summary, infer_case_issue
from api.osc.utils import (
    _osc_json_value,
    _osc_parse_dt,
    _osc_web_connect,
    # Re-exported: other modules import these from judicial.py
    _osc_fetch_fulltext_from_exact_case_search,
    _osc_fetch_fulltext_from_judicial,
    _osc_pick_best_manifest_item,
    _osc_summarize_legal_insight,
)
from api.osc.insight_filters import (
    displayable_insight_item,
    is_extractive_fast_judgment_digest,
    is_non_extractable_legal_insight,
)
from api.runtime_paths import get_judgments_json_path

logger = logging.getLogger(__name__)


def _osc_show_fast_insight_candidates() -> bool:
    return str(os.environ.get("MAGI_SHOW_FAST_INSIGHT_CANDIDATES", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _osc_show_external_insight_candidates() -> bool:
    return str(os.environ.get("MAGI_SHOW_EXTERNAL_INSIGHT_CANDIDATES", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _osc_probable_fast_judgment_candidate(summary: str, source_url: str = "", title: str = "") -> bool:
    if is_extractive_fast_judgment_digest(summary, title):
        return True
    url = str(source_url or "").strip().lower()
    return ("dr-lawbot.com" in url or "tlr." in url) and len(str(summary or "").strip()) < 280


def _osc_court_summary_displayable(row: dict | None) -> bool:
    """Return True only for source-bound summaries safe for OSC and drafts.

    The old OSC flow fetched the newest 500 rows first and filtered them only
    for a few placeholder strings.  A burst of degraded summaries could
    therefore hide older, valid opinions and could also expose fluent but
    source-mismatched text.  Keep this gate shared by list and detail routes so
    the browser never sees a weaker contract than the summarisation pipeline.
    """

    if not isinstance(row, dict):
        return False
    summary = str(row.get("summary") or "").strip()
    source = str(row.get("full_text") or "").strip()
    if not summary or not source:
        return False
    if is_non_extractable_legal_insight(
        row.get("court_name"),
        row.get("case_number"),
        row.get("case_type"),
        summary,
        source,
    ):
        return False
    issue = infer_case_issue(
        source,
        str(row.get("case_number") or ""),
        str(row.get("case_type") or ""),
    )
    quality = evaluate_practice_ready_summary(
        summary,
        source,
        issue,
        str(row.get("court_name") or ""),
        min_score=max(70, min(100, int(os.environ.get("MAGI_OSC_INSIGHT_MIN_SCORE", "80") or "80"))),
    )
    return bool(quality.ok)


def _osc_insight_merge_key(item: dict) -> tuple[str, str]:
    """Return a conservative key for deduplicating judgment snapshots.

    Human-reviewed ``legal_insights`` rows may deliberately contain more than
    one proposition extracted from the same judgment, so they remain distinct.
    Judgment and legacy-JSON projections, however, often carry the same source
    URL or full text and should not consume the public 500-row budget twice.
    """

    source_type = str(item.get("source_type") or "").strip()
    if source_type == "legal_insights":
        return ("legal_insights", str(item.get("id") or ""))
    url = str(item.get("url") or "").strip().lower().rstrip("/")
    if url:
        return ("judgment_url", url)
    full_text = re.sub(r"\s+", "", str(item.get("full_text") or "").strip())
    if full_text:
        return ("judgment_text", hashlib.sha256(full_text.encode("utf-8")).hexdigest())
    identity = "|".join(
        re.sub(r"\s+", "", str(item.get(field) or "").strip()).lower()
        for field in ("court", "case_number", "title", "summary")
    )
    return ("judgment_identity", hashlib.sha256(identity.encode("utf-8")).hexdigest())


# ---------------------------------------------------------------------------
# _osc_collect_insights
# ---------------------------------------------------------------------------

def _osc_collect_insights():
    items = []
    conn, _cfg = _osc_web_connect()
    cur = conn.cursor(dictionary=True)
    try:
        try:
            cur.execute(
                """
                SELECT id, case_number, document_name, court_reference, court_type,
                       insight_type, insight_text, case_reason, source_file, extracted_date, raw_text,
                       COALESCE(is_degraded, 0) AS is_degraded
                FROM legal_insights
                WHERE TRIM(COALESCE(insight_text, '')) <> ''
                  AND COALESCE(is_degraded, 0)=0
                ORDER BY extracted_date DESC, id DESC
                LIMIT 1500
                """
            )
            for r in (cur.fetchall() or []):
                if len(items) >= 500:
                    break
                title = (r.get("document_name") or r.get("insight_type") or "實務見解").strip()
                # insight_text = 結構化法律見解萃取結果；raw_text = 判決原文
                insight_text = (r.get("insight_text") or "").strip()
                raw_text = (r.get("raw_text") or "").strip()
                if is_extractive_fast_judgment_digest(title, insight_text, raw_text) and not _osc_show_fast_insight_candidates():
                    continue
                if is_non_extractable_legal_insight(
                    title,
                    r.get("court_reference"),
                    insight_text,
                    raw_text,
                    r.get("case_reason"),
                ):
                    continue
                full_text = raw_text or insight_text
                summary = (insight_text or full_text[:500])[:350]
                ts = r.get("extracted_date")
                source_file = str(r.get("source_file") or "").strip()
                source_url = source_file if source_file.lower().startswith(("http://", "https://")) else ""
                item = {
                    "id": f"li-{r.get('id')}",
                    "source_type": "legal_insights",
                    "source": "見解庫",
                    "title": title,
                    "summary": summary,
                    "insight_text": insight_text,
                    "full_text": full_text,
                    "url": source_url,
                    "case_number": r.get("case_number") or "",
                    "case_reason": r.get("case_reason") or "",
                    "court": r.get("court_reference") or r.get("court_type") or "",
                    "verification_state": (
                        LOCAL_REVIEWED
                        if str(r.get("insight_type") or "").strip().lower()
                        in {"manual", "human_reviewed", "lawyer_reviewed"}
                        else "unverified"
                    ),
                    "draft_eligible": str(r.get("insight_type") or "").strip().lower()
                    in {"manual", "human_reviewed", "lawyer_reviewed"},
                    "timestamp": _osc_json_value(ts) if ts else "",
                    "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
                }
                if displayable_insight_item(item):
                    items.append(item)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_collect_insights:legal_insights", exc_info=True)

        try:
            cur.execute(
                """
                SELECT id, jid, court_name, case_number, case_type, judgment_date,
                       summary, full_text, source_url, crawled_at
                FROM court_judgments
                WHERE TRIM(COALESCE(summary, '')) <> ''
                  AND CHAR_LENGTH(summary) >= 80
                  AND summary LIKE '%## 實務見解%'
                  AND summary NOT LIKE '%抽取式快篩%'
                ORDER BY crawled_at DESC, id DESC
                LIMIT 1500
                """
            )
            for r in (cur.fetchall() or []):
                if len(items) >= 500:
                    break
                title = f"{(r.get('court_name') or '').strip()} {(r.get('case_number') or '').strip()}".strip() or "裁判見解"
                if not _osc_court_summary_displayable(r):
                    continue
                source_url = str(r.get("source_url") or "").strip()
                source_url_lower = source_url.lower()
                is_external_cache = "dr-lawbot.com" in source_url_lower or "tlr." in source_url_lower
                if is_external_cache and not _osc_show_external_insight_candidates():
                    # External semantic hits belong to discovery, not the formal
                    # practical-insight library, until an official local copy
                    # has been matched and reviewed.
                    continue
                full_text = (r.get("full_text") or r.get("summary") or "").strip()
                summary = (r.get("summary") or full_text[:350] or "").strip()
                is_fast_digest = _osc_probable_fast_judgment_candidate(summary, source_url, title)
                if is_fast_digest and not _osc_show_fast_insight_candidates():
                    continue
                ts = r.get("crawled_at") or r.get("judgment_date")
                item = {
                    "id": f"cj-{r.get('id')}",
                    "source_type": "court_judgments",
                    "source": "裁判書（抽取式快篩）" if is_fast_digest else "裁判書",
                    "title": title,
                    "summary": summary,
                    "full_text": full_text,
                    "url": source_url,
                    "case_number": r.get("case_number") or "",
                    "case_reason": r.get("case_type") or "",
                    "court": r.get("court_name") or "",
                    "quality": "fast_extractive" if is_fast_digest else "authoritative_summary",
                    "verification_state": (
                        EXTERNAL_CANDIDATE
                        if is_external_cache
                        else VERIFIED_LOCAL
                    ),
                    "draft_eligible": (
                        not is_fast_digest
                        and not is_external_cache
                    ),
                    "timestamp": _osc_json_value(ts) if ts else "",
                    "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
                }
                if displayable_insight_item(item):
                    items.append(item)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_collect_insights:court_judgments", exc_info=True)
    finally:
        try:
            cur.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_collect_insights:cur_close", exc_info=True)
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_collect_insights:conn_close", exc_info=True)

    # Merge legacy judgments json so old workflow remains visible.
    try:
        json_path = get_judgments_json_path()
        if json_path.exists():
            with json_path.open("r", encoding="utf-8") as f:
                data = json.load(f) or []
            if isinstance(data, list):
                for i, r in enumerate(data):
                    if not isinstance(r, dict):
                        continue
                    full_text = (r.get("full_text") or r.get("summary") or "").strip()
                    summary = (r.get("summary") or "")[:350]
                    if _osc_probable_fast_judgment_candidate(summary or full_text, r.get("url") or "", r.get("title") or "") and not _osc_show_fast_insight_candidates():
                        continue
                    if is_non_extractable_legal_insight(
                        r.get("title"),
                        summary,
                        full_text,
                        r.get("case_reason"),
                        r.get("court_name"),
                    ):
                        continue
                    ts = r.get("timestamp")
                    item = {
                        "id": f"json-{i}",
                        "source_type": "judgments_json",
                        "source": r.get("source") or "爬蟲快照",
                        "title": r.get("title") or "裁判資料",
                        "summary": summary,
                        "full_text": full_text,
                        "url": r.get("url") or "",
                        "case_number": r.get("case_number") or "",
                        "case_reason": r.get("case_reason") or "",
                        "court": r.get("court_name") or "",
                        "verification_state": "unverified",
                        "draft_eligible": False,
                        "timestamp": ts or "",
                        "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
                    }
                    if displayable_insight_item(item):
                        items.append(item)
    except Exception as e:
        logger.warning(f"osc insights json merge failed: {e}")

    items.sort(key=lambda x: x.get("sort_ts") or 0, reverse=True)
    merged = []
    seen = set()
    for it in items:
        key = _osc_insight_merge_key(it)
        if key in seen:
            continue
        seen.add(key)
        it.pop("sort_ts", None)
        merged.append(it)
        if len(merged) >= 500:
            break
    return merged


# ---------------------------------------------------------------------------
# Document-kind helpers
# ---------------------------------------------------------------------------

_OSC_DOC_KIND_KEYWORDS = {
    "all": [],
    "pleading": ["書狀", "起訴狀", "答辯狀", "準備狀", "聲請", "陳報狀", "上訴狀", "抗告狀", "狀"],
    "poa": ["委任", "委託", "委任狀", "委任书", "委託書"],
    "receipt": ["收據", "收执", "收執", "繳費", "訴訟中費用", "粉紅"],
    "laf": ["法扶", "法律扶助", "接案通知", "開辦資料", "開辦通知"],
    "judgment": ["判決", "裁定", "調解不成立", "和解", "決定書"],
    "court_notice": ["通知", "庭期", "開庭", "法院通知"],
}


def _osc_doc_kind_match(kind: str, blob: str) -> bool:
    k = str(kind or "all").strip().lower()
    if k in {"", "all"}:
        return True
    kws = _OSC_DOC_KIND_KEYWORDS.get(k, [])
    if not kws:
        return True
    b = str(blob or "")
    return any(x in b for x in kws)


def _osc_doc_kind_label(blob: str) -> str:
    b = str(blob or "")
    if _osc_doc_kind_match("pleading", b):
        return "書狀"
    if _osc_doc_kind_match("poa", b):
        return "委任狀/委託書"
    if _osc_doc_kind_match("receipt", b):
        return "收據/繳費"
    if _osc_doc_kind_match("laf", b):
        return "法扶資料"
    if _osc_doc_kind_match("court_notice", b):
        return "法院通知"
    if _osc_doc_kind_match("judgment", b):
        return "判決/裁定"
    return "一般文件"
