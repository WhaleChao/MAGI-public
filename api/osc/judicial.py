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
import logging
import os

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
    mark_extractive_fast_digest_summary,
)

logger = logging.getLogger(__name__)


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
                       insight_type, insight_text, case_reason, source_file, extracted_date, raw_text
                FROM legal_insights
                ORDER BY extracted_date DESC, id DESC
                LIMIT 500
                """
            )
            for r in (cur.fetchall() or []):
                title = (r.get("document_name") or r.get("insight_type") or "實務見解").strip()
                # insight_text = 結構化法律見解萃取結果；raw_text = 判決原文
                insight_text = (r.get("insight_text") or "").strip()
                raw_text = (r.get("raw_text") or "").strip()
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
                ORDER BY crawled_at DESC, id DESC
                LIMIT 500
                """
            )
            for r in (cur.fetchall() or []):
                title = f"{(r.get('court_name') or '').strip()} {(r.get('case_number') or '').strip()}".strip() or "裁判見解"
                if not (r.get("summary") or "").strip():
                    continue
                full_text = (r.get("full_text") or r.get("summary") or "").strip()
                summary = (r.get("summary") or full_text[:350] or "").strip()
                is_fast_digest = is_extractive_fast_judgment_digest(summary)
                if is_non_extractable_legal_insight(
                    title,
                    summary,
                    full_text,
                    r.get("case_type"),
                    r.get("court_name"),
                ):
                    continue
                ts = r.get("crawled_at") or r.get("judgment_date")
                item = {
                    "id": f"cj-{r.get('id')}",
                    "source_type": "court_judgments",
                    "source": "裁判書（抽取式快篩）" if is_fast_digest else "裁判書",
                    "title": title,
                    "summary": mark_extractive_fast_digest_summary(summary) if is_fast_digest else summary,
                    "full_text": full_text,
                    "url": r.get("source_url") or "",
                    "case_number": r.get("case_number") or "",
                    "case_reason": r.get("case_type") or "",
                    "court": r.get("court_name") or "",
                    "quality": "fast_extractive" if is_fast_digest else "authoritative_summary",
                    "draft_eligible": not is_fast_digest,
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
        json_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "skills", "judgment-collector", "judgments.json")
        )
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f) or []
            if isinstance(data, list):
                for i, r in enumerate(data):
                    if not isinstance(r, dict):
                        continue
                    full_text = (r.get("full_text") or r.get("summary") or "").strip()
                    summary = (r.get("summary") or "")[:350]
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
                        "timestamp": ts or "",
                        "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
                    }
                    if displayable_insight_item(item):
                        items.append(item)
    except Exception as e:
        logger.warning(f"osc insights json merge failed: {e}")

    items.sort(key=lambda x: x.get("sort_ts") or 0, reverse=True)
    for it in items:
        it.pop("sort_ts", None)
    return items


# ---------------------------------------------------------------------------
# Document-kind helpers
# ---------------------------------------------------------------------------

_OSC_DOC_KIND_KEYWORDS = {
    "all": [],
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
