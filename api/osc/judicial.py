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

import difflib
import json
import logging
import os
import re

from api.osc.utils import (
    _osc_exec,
    _osc_run_skill,
    _osc_parse_skill_output,
    _osc_load_judicial_search_results,
    _osc_pick_exact_judicial_search_result,
    _osc_parse_structured_case_spec,
    _osc_unique_keep_order,
    _osc_fetch_url_text,
    _osc_json_value,
    _osc_parse_dt,
    _osc_norm_path,
    _osc_local_path_candidates,
    _osc_title_norm,
    _osc_extract_case_markers,
    _osc_normalize_court_name,
    _osc_normalize_case_word,
    _osc_extract_court_names,
    _osc_skill_json_task,
    _osc_web_connect,
    _osc_pick_best_manifest_item as _osc_pick_best_manifest_item_util,
    _OSC_JUDICIAL_COURT_SEARCH_LABELS,
)

try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _osc_fetch_fulltext_from_exact_case_search
# ---------------------------------------------------------------------------

def _osc_fetch_fulltext_from_exact_case_search(*, title: str = "", case_number: str = "", timeout_sec: int = 180) -> dict:
    target = _osc_parse_structured_case_spec(title=title, case_number=case_number)
    if not (target.get("case_year") and target.get("case_word") and target.get("case_no")):
        return {"ok": False, "error": "structured_case_unavailable"}

    search_payload = {
        "keywords": "",
        "max_results": 20,
        "headless": True,
        "timeout_sec": max(60, min(240, int(timeout_sec))),
        "case_year": target.get("case_year") or "",
        "case_word": target.get("case_word") or "",
        "case_no": target.get("case_no") or "",
    }
    search_courts = [
        _OSC_JUDICIAL_COURT_SEARCH_LABELS.get(court_name, court_name)
        for court_name in (target.get("courts") or [])
        if str(court_name or "").strip()
    ]
    search_courts = _osc_unique_keep_order(search_courts)
    if search_courts:
        search_payload["courts"] = search_courts

    rr = _osc_run_skill(
        "judicial-web-search",
        _osc_skill_json_task("search", search_payload),
        timeout_sec=max(120, int(timeout_sec) + 60),
        route_key="osc:insights:fetch_full:exact_case_search",
    )
    rp = _osc_parse_skill_output(rr)
    if not rp.get("success"):
        return {"ok": False, "error": rp.get("error") or "judicial_exact_search_failed"}

    items = _osc_load_judicial_search_results(rp)
    best = _osc_pick_exact_judicial_search_result(items, title=title, case_number=case_number)
    if not best:
        return {"ok": False, "error": "judicial_exact_case_not_found"}

    fetch_payload = {
        "url": str(best.get("url") or "").strip(),
        "headless": True,
        "timeout_sec": max(45, min(180, int(timeout_sec))),
        "max_chars": 180000,
    }
    rr2 = _osc_run_skill(
        "judicial-web-search",
        _osc_skill_json_task("fetch_text", fetch_payload),
        timeout_sec=max(120, int(timeout_sec) + 60),
        route_key="osc:insights:fetch_full:exact_case_fetch",
    )
    rp2 = _osc_parse_skill_output(rr2)
    text = ""
    text_path = str(rp2.get("text_path") or "").strip() if isinstance(rp2, dict) else ""
    if text_path and os.path.exists(text_path):
        try:
            with open(text_path, "r", encoding="utf-8", errors="replace") as f:
                text = (f.read() or "").strip()
        except Exception:
            text = ""
    if (not text) and isinstance(rp2, dict):
        text = str(rp2.get("text") or "").strip()
    if (not text) and fetch_payload["url"]:
        direct = _osc_fetch_url_text(fetch_payload["url"], timeout=max(20, min(60, int(timeout_sec))))
        if direct.get("ok"):
            text = str(direct.get("text") or "").strip()
    if len(text) < 120:
        return {"ok": False, "error": "judicial_exact_case_text_not_found"}

    return {
        "ok": True,
        "source": "fallback_judicial_exact_case",
        "text": text,
        "matched": {
            "title": str(best.get("title") or ""),
            "url": str(best.get("url") or ""),
            "case_number_query": str(target.get("case_number_query") or ""),
        },
    }


# ---------------------------------------------------------------------------
# _osc_pick_best_manifest_item
# ---------------------------------------------------------------------------

def _osc_pick_best_manifest_item(items: list[dict], *, title: str = "", case_number: str = "") -> dict:
    if not isinstance(items, list) or not items:
        return {}
    target_title = str(title or "").strip()
    target_case = re.sub(r"\s+", "", str(case_number or ""))
    target_norm = _osc_title_norm(target_title)
    target_markers = _osc_extract_case_markers(target_title + " " + target_case)
    best = None
    best_score = -1.0
    for it in items:
        if not isinstance(it, dict):
            continue
        p = str(it.get("archived_text_path") or it.get("text_path") or "").strip()
        if not p or (not os.path.exists(p)):
            continue
        title2 = str(it.get("title") or "").strip()
        score = 0.0
        item_markers = _osc_extract_case_markers(title2)
        if target_case and (target_case in re.sub(r"\s+", "", title2)):
            score += 6.0
        if target_markers and item_markers:
            inter = target_markers.intersection(item_markers)
            if inter:
                score += 5.0
        n2 = _osc_title_norm(title2)
        if target_norm and n2:
            score += difflib.SequenceMatcher(None, target_norm, n2).ratio() * 2.5
        score += min(float(os.path.getsize(p) if os.path.exists(p) else 0) / 200000.0, 0.8)
        if score > best_score:
            best_score = score
            best = it
    if (not best) or best_score < 1.0:
        return {}
    out = dict(best)
    out["match_score"] = round(best_score, 4)
    return out


# ---------------------------------------------------------------------------
# _osc_summarize_legal_insight
# ---------------------------------------------------------------------------

def _osc_summarize_legal_insight(full_text: str) -> str:
    text = str(full_text or "").strip()
    if not text:
        return ""
    prompt = (
        "你是臺灣法律實務見解萃取器。\n"
        "以下是一份裁判全文，請從中萃取「法院對法律問題的解釋與見解」，\n"
        "重點不是判決結果（誰勝誰敗、刑度多少），而是法院在判決理由中\n"
        "對法律爭點的論述、法條的解釋適用、以及可供其他案件援引的法律見解。\n\n"
        "輸出語言：繁體中文（臺灣用語）。\n"
        "輸出格式固定為：\n"
        "1) 法律爭點：本案涉及哪些法律問題\n"
        "2) 法院見解：法院對各爭點的法律解釋與論理（逐點摘錄，保留原文關鍵用語）\n"
        "3) 可援引要旨（條列）：可直接引用於書狀中的法院見解要旨，每條標註出處段落\n\n"
        "注意：\n"
        "- 不要摘要案件事實經過或判決主文\n"
        "- 聚焦於法院的法律論理、法條解釋、證據法則適用等「見解」部分\n"
        "- 若判決中引用其他判例或決議，請一併摘錄\n"
        "- 不要回覆語言偏好確認，不要加入前言，請勿杜撰。\n\n"
        f"【全文開始】\n{text[:180000]}\n【全文結束】"
    )
    bad_markers = ("近期行程", "婦女節", "確認鄭羢允案", "法扶開辦末日")

    def _clean_output(raw: str) -> str:
        cleaned = str(raw or "").strip()
        if not cleaned:
            return ""
        if _normalize_output_text:
            try:
                cleaned = _normalize_output_text(cleaned, platform="WEB")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_summarize_legal_insight:_clean_output", exc_info=True)
        return cleaned.strip()

    def _usable(raw: str) -> bool:
        cleaned = _clean_output(raw)
        if not cleaned:
            return False
        if any(marker in cleaned for marker in bad_markers):
            return False
        if "爭點" in cleaned and ("法院見解" in cleaned or "可援引" in cleaned or "可直接引用" in cleaned):
            return True
        return len(cleaned) >= 80

    try:
        from skills.bridge.inference_gateway import InferenceGateway
        _gw = InferenceGateway()

        rr = _gw.chat(
            prompt,
            task_type="legal_analysis",
            timeout=int(os.environ.get("OSC_INSIGHT_SUMMARY_TIMEOUT_SEC", "120") or "120"),
        )
        out = _clean_output(rr.get("response") or "")
        if rr.get("success") and _usable(out):
            return out
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_summarize_legal_insight:primary", exc_info=True)

    # Fallback: try shorter prompt
    fallback_prompt = (
        "請從以下裁判全文中萃取法院的法律見解（非判決結果）。\n"
        "格式：1) 法律爭點 2) 法院見解 3) 可援引要旨（條列）\n"
        "輸出繁體中文，不要摘要事實經過。\n\n"
        f"【全文開始】\n{text[:60000]}\n【全文結束】"
    )
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        _gw2 = InferenceGateway()

        rr = _gw2.chat(
            fallback_prompt,
            task_type="legal_analysis",
            timeout=int(os.environ.get("OSC_INSIGHT_SUMMARY_FALLBACK_TIMEOUT_SEC", "120") or "120"),
        )
        out = _clean_output(rr.get("response") or "")
        if rr.get("success") and _usable(out):
            return out
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_summarize_legal_insight:fallback", exc_info=True)

    return "摘要失敗：本地摘要模型未產出可用內容。"


# ---------------------------------------------------------------------------
# _osc_fetch_fulltext_from_judicial
# ---------------------------------------------------------------------------

def _osc_fetch_fulltext_from_judicial(*, title: str = "", case_number: str = "", case_reason: str = "", timeout_sec: int = 180) -> dict:
    """
    來源被登入保護/反爬阻擋時，
    先走司法院案號精準查詢，再退回司法院全文搜尋歸檔，
    最後才退回 judgment-collector。
    """
    reason = (title or "").strip() or (case_number or "").strip() or (case_reason or "").strip()
    if not reason:
        return {"ok": False, "error": "missing_query"}
    archive_payload = {
        "query": reason,
        "max_results": 5,
        "max_chars": 180000,
        "headless": True,
        "timeout_sec": max(90, min(240, int(timeout_sec))),
    }
    collect_payload = {
        "case_reason": reason,
        "case_number": (case_number or "").strip(),
        "max_results": 5,
        "max_chars": 180000,
        "headless": True,
        "timeout_sec": max(90, min(420, int(timeout_sec))),
        "save_to_db": True,
        "notify": False,
    }

    def _try_skill(skill: str, task: str, *, route_key: str, ok_source: str, summary_source: str, inline_source: str) -> dict:
        rr = _osc_run_skill(
            skill,
            task,
            timeout_sec=max(120, int(timeout_sec) + 60),
            route_key=route_key,
        )
        rp = _osc_parse_skill_output(rr)
        if not rp.get("success"):
            return {"ok": False, "error": rp.get("error") or f"{skill}_failed"}
        items = []
        manifest_path = str(rp.get("manifest_path") or "").strip()
        if manifest_path and os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    mf = json.load(f) or {}
                items = mf.get("items") or []
            except Exception:
                items = []
        if not items:
            items = rp.get("items_preview") or rp.get("items") or []
        best = _osc_pick_best_manifest_item(items, title=title, case_number=case_number)
        path = str(best.get("archived_text_path") or best.get("text_path") or "").strip()
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = (f.read() or "").strip()
                if len(text) >= 120:
                    return {
                        "ok": True,
                        "source": ok_source,
                        "text": text,
                        "matched": {
                            "title": str(best.get("title") or ""),
                            "url": str(best.get("url") or ""),
                            "path": path,
                        },
                    }
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_fetch_fulltext_from_judicial:_try_skill:read_path", exc_info=True)
        summary_path = str(rp.get("summary_path") or "").strip()
        if summary_path and os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8", errors="replace") as f:
                    text = (f.read() or "").strip()
                if len(text) >= 120:
                    return {"ok": True, "source": summary_source, "text": text}
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_osc_fetch_fulltext_from_judicial:_try_skill:read_summary", exc_info=True)
        for it in (items or []):
            txt = str(it.get("full_text") or it.get("text") or it.get("summary") or "").strip()
            if len(txt) >= 300:
                return {"ok": True, "source": inline_source, "text": txt}
        return {"ok": False, "error": f"{skill}_fulltext_not_found"}

    last_error = "judicial_fulltext_not_found"

    exact = _osc_fetch_fulltext_from_exact_case_search(
        title=title,
        case_number=case_number,
        timeout_sec=max(90, min(240, int(timeout_sec))),
    )
    if exact.get("ok"):
        return exact
    last_error = str(exact.get("error") or last_error)

    archive = _try_skill(
        "judicial-flow-search-archive",
        _osc_skill_json_task("search_archive", archive_payload),
        route_key="osc:insights:fetch_full:search_archive",
        ok_source="fallback_judicial_archive",
        summary_source="fallback_judicial_archive_summary",
        inline_source="fallback_judicial_archive_inline",
    )
    if archive.get("ok"):
        return archive
    last_error = str(archive.get("error") or last_error)

    collector = _try_skill(
        "judgment-collector",
        _osc_skill_json_task("collect", collect_payload),
        route_key="osc:insights:fetch_full:judgment_collector",
        ok_source="fallback_judgment_collector",
        summary_source="fallback_judgment_collector_summary",
        inline_source="fallback_judgment_collector_inline",
    )
    if collector.get("ok"):
        return collector
    last_error = str(collector.get("error") or last_error)
    return {"ok": False, "error": last_error}


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
                full_text = raw_text or insight_text
                summary = (insight_text or full_text[:500])[:350]
                ts = r.get("extracted_date")
                source_file = str(r.get("source_file") or "").strip()
                source_url = source_file if source_file.lower().startswith(("http://", "https://")) else ""
                items.append(
                    {
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
                )
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
                full_text = (r.get("full_text") or r.get("summary") or "").strip()
                summary = (r.get("summary") or full_text[:350] or "").strip()
                ts = r.get("crawled_at") or r.get("judgment_date")
                items.append(
                    {
                        "id": f"cj-{r.get('id')}",
                        "source_type": "court_judgments",
                        "source": "裁判書",
                        "title": title,
                        "summary": summary,
                        "full_text": full_text,
                        "url": r.get("source_url") or "",
                        "case_number": r.get("case_number") or "",
                        "case_reason": r.get("case_type") or "",
                        "court": r.get("court_name") or "",
                        "timestamp": _osc_json_value(ts) if ts else "",
                        "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
                    }
                )
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
                    ts = r.get("timestamp")
                    items.append(
                        {
                            "id": f"json-{i}",
                            "source_type": "judgments_json",
                            "source": r.get("source") or "爬蟲快照",
                            "title": r.get("title") or "裁判資料",
                            "summary": (r.get("summary") or "")[:350],
                            "full_text": full_text,
                            "url": r.get("url") or "",
                            "case_number": r.get("case_number") or "",
                            "case_reason": r.get("case_reason") or "",
                            "court": r.get("court_name") or "",
                            "timestamp": ts or "",
                            "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
                        }
                    )
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
