"""
Judgment collection & search operations extracted from Orchestrator.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import json
import importlib.util
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from api.legal_workflow import append_workflow_footer, detect_legal_workflow
from api.osc.insight_filters import is_extractive_fast_judgment_digest
from api.osc.taiwan_legal_mcp import (
    call_taiwan_legal_tool,
    merge_judgment_sources,
    search_practical_judgments_via_mcp,
    taiwan_legal_mcp_available,
    taiwan_legal_mcp_enabled,
)
from api.osc.tw_legal_rag import (
    search_practical_judgments_via_tlr,
    tw_legal_rag_enabled,
)

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_local_db_manager() -> Optional[Any]:
    """Return a DB manager that can query the local judgment archive reliably."""
    osc_compat_path = os.path.join(_MAGI_ROOT, "osc.py")
    try:
        if os.path.isfile(osc_compat_path):
            spec = importlib.util.spec_from_file_location("magi_osc_compat", osc_compat_path)
            if spec and spec.loader:
                osc_compat = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(osc_compat)
                osc_db_manager = getattr(osc_compat, "DatabaseManager", None)
                if osc_db_manager is not None:
                    return osc_db_manager(
                        {
                            "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
                            "port": int(os.environ.get("OSC_DB_PORT", "3306") or "3306"),
                            "user": os.environ.get("OSC_DB_USER", "python_user"),
                            "password": os.environ.get("OSC_DB_PASSWORD", ""),
                            "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
                        }
                    )
    except Exception as exc:
        logger.debug("osc compat db manager unavailable: %s", exc, exc_info=True)

    try:
        if _MAGI_ROOT not in sys.path:
            sys.path.insert(0, _MAGI_ROOT)
        from osc import DatabaseManager as OscDatabaseManager

        return OscDatabaseManager(
            {
                "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
                "port": int(os.environ.get("OSC_DB_PORT", "3306") or "3306"),
                "user": os.environ.get("OSC_DB_USER", "python_user"),
                "password": os.environ.get("OSC_DB_PASSWORD", ""),
                "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
            }
        )
    except Exception as exc:
        logger.warning("local judgment DB manager unavailable: %s", exc)
        return None


def extract_judgment_collect_payload(message: str) -> tuple[Optional[dict], str]:
    text = str(message or "").strip()
    if not text:
        return None, "\U0001f50e \u8acb\u63d0\u4f9b\u6848\u7531\u6216\u6848\u865f\uff0c\u4f8b\u5982\uff1a`\u67e5\u5224\u6c7a \u50b7\u5bb3`\u3001`\u67e5\u5224\u6c7a 113\u5e74\u5ea6\u4e0a\u8a34\u5b57\u7b2c12\u865f`"

    raw = re.sub(r"^@MAGI\s*", "", text, flags=re.IGNORECASE).strip()
    for _ in range(3):
        prev = raw
        raw = re.sub(r"^(?:\u5e6b\u6211|\u8acb|\u9ebb\u7169|\u5e6b\u5fd9|\u53ef\u4ee5\u5e6b\u6211|\u5354\u52a9\u6211)\s*", "", raw).strip()
        raw = re.sub(
            r"^(?:\u67e5\u5224\u6c7a|\u627e\u5224\u6c7a|\u5224\u6c7a\u641c\u5c0b|\u641c\u5c0b\u5224\u6c7a|\u6536\u96c6\u5224\u6c7a|\u5224\u6c7a\u641c\u96c6|\u641c\u5c0b\u6700\u9ad8\u6cd5\u9662\u5224\u6c7a|\u5be6\u52d9\u898b\u89e3|\u6cd5\u5f8b\u898b\u89e3|\u6cd5\u9662\u898b\u89e3)\s*",
            "",
            raw,
        ).strip()
        raw = re.sub(r"^(?:\u67e5\u4e00\u4e0b|\u627e\u4e00\u4e0b|\u641c\u5c0b\u4e00\u4e0b|\u641c\u4e00\u4e0b)\s*", "", raw).strip()
        if raw == prev:
            break
    raw = raw.strip(" \uff1a:\uff0c,\u3002\uff1b;")

    case_match = re.search(
        r"(\d{4}-\d{4}|\d{2,3}\u5e74\u5ea6[^\s]{1,12}\u5b57\u7b2c?\d+\u865f?)",
        raw,
    )
    if case_match:
        return {"case_number": case_match.group(1).strip()}, ""

    reason = re.sub(r"^(?:\u6700\u8fd1\u7684?|\u6700\u65b0\u7684?|\u6700\u9ad8\u6cd5\u9662\u7684?|\u6cd5\u9662\u7684?)", "", raw).strip()
    reason = re.sub(r"(?:\u7684)?(?:\u6cd5\u9662)?\u5224\u6c7a$", "", reason).strip(" \uff1a:\uff0c,\u3002\uff1b;")
    reason = re.sub(r"\s+", " ", reason).strip()

    generic_only = {
        "\u6700\u8fd1", "\u6700\u65b0", "\u6cd5\u9662", "\u5224\u6c7a", "\u6700\u8fd1\u5224\u6c7a", "\u6700\u65b0\u5224\u6c7a",
        "\u6cd5\u9662\u5224\u6c7a", "\u6700\u8fd1\u6cd5\u9662\u5224\u6c7a", "\u6700\u8fd1\u7684\u6cd5\u9662\u5224\u6c7a",
        "\u6700\u65b0\u6cd5\u9662\u5224\u6c7a", "\u6700\u65b0\u7684\u6cd5\u9662\u5224\u6c7a", "\u6700\u9ad8\u6cd5\u9662\u5224\u6c7a",
    }
    if not reason or len(reason) < 2 or reason in generic_only:
        return None, "\U0001f50e \u8acb\u63d0\u4f9b\u6848\u7531\u6216\u6848\u865f\uff0c\u4f8b\u5982\uff1a`\u67e5\u5224\u6c7a \u50b7\u5bb3`\u3001`\u67e5\u5224\u6c7a 113\u5e74\u5ea6\u4e0a\u8a34\u5b57\u7b2c12\u865f`"
    return {"case_reason": reason}, ""


def format_judgment_collect_result(payload: dict) -> str:
    if not isinstance(payload, dict):
        return "\u274c \u5224\u6c7a\u641c\u5c0b\u5931\u6557\uff1a\u56de\u50b3\u683c\u5f0f\u7570\u5e38"
    if not payload.get("success"):
        err = str(payload.get("error") or "unknown").strip()
        return f"\u274c \u5224\u6c7a\u641c\u5c0b\u5931\u6557\uff1a{err}"

    reason = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
    _reason_label = reason or "\u6848\u4ef6"
    lines = [f"\U0001f4da \u5224\u6c7a\u641c\u5c0b\u5b8c\u6210\uff1a{_reason_label}"]
    source_label = str(payload.get("source_label") or "").strip()
    if source_label:
        lines.append(f"來源：{source_label}")
    court_level = str(payload.get("court_level") or "").strip()
    if court_level:
        lines.append(f"\u6cd5\u9662\uff1a{court_level}")
    count = payload.get("count")
    if count is not None:
        lines.append(f"\u6536\u96c6\u7b46\u6578\uff1a{count}")

    LINE_MSG_BUDGET = 4500
    header_len = len("\n".join(lines)) + 2
    remaining = LINE_MSG_BUDGET - header_len

    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    items = _high_quality_judgment_items(raw_items)
    reject_counts = _judgment_quality_rejection_counts(raw_items)
    rejected_total = sum(reject_counts.values())
    if rejected_total:
        lines.append(
            "已排除低品質候選："
            f"抽取式快篩 {reject_counts.get('fast_extractive', 0)}、"
            f"降級摘要 {reject_counts.get('degraded', 0)}、"
            f"缺摘要 {reject_counts.get('empty_summary', 0)}"
        )
    if raw_items and not items:
        lines.append("\n已找到候選裁判，但目前只有抽取式快篩或品質未通過摘要；MAGI 已阻擋其作為正式見解。")
        lines.append("請改用實務見解精查、TLR 全文檢索，或稍後讓夜間重摘要補齊。")
        return "\n".join(lines)
    for row in items:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        summary = str(row.get("summary_full") or row.get("summary_preview") or "").strip()
        is_degraded = row.get("is_degraded", False)

        entry_lines = [f"\n{'=' * 30}", f"\u3010{title[:80]}\u3011"]
        if row.get("url"):
            entry_lines.append(str(row["url"]))
        if summary and not is_degraded:
            if len(summary) > 600:
                summary = summary[:600] + "\u2026\uff08\u5b8c\u6574\u5167\u5bb9\u898b\u5831\u544a\uff09"
            entry_lines.append(summary)
        elif is_degraded and summary:
            entry_lines.append(f"[\u6458\u8981\u54c1\u8cea\u4e0d\u4f73\uff0c\u5f85\u91cd\u8a66]\n{summary[:200]}\u2026")
        else:
            entry_lines.append("[\u5c1a\u7121\u6458\u8981]")

        entry_text = "\n".join(entry_lines)
        if len(entry_text) > remaining:
            _shown = len([l for l in lines if l.startswith("\u3010")])
            lines.append(f"\n\u2026\u5176\u9918 {len(items) - _shown} \u7b46\u8acb\u898b\u5831\u544a\u6a94\u6848")
            break
        lines.append(entry_text)
        remaining -= len(entry_text)

    retry_queued_count = payload.get("retry_queued_count")
    if retry_queued_count:
        lines.append(f"\n\u6458\u8981\u91cd\u8a66\u4f47\u5217\uff1a+{retry_queued_count}")
    return "\n".join(lines)


def _judgment_item_quality_issue(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return "empty_summary"
    summary = str(
        item.get("summary_full")
        or item.get("summary_preview")
        or item.get("summary")
        or item.get("insight_text")
        or item.get("full_text_excerpt")
        or ""
    ).strip()
    title = str(item.get("title") or item.get("citation_text") or "").strip()
    url = str(item.get("url") or item.get("source_url") or "").strip().lower()
    if item.get("is_degraded") or "系統降級回覆" in summary:
        return "degraded"
    if item.get("is_fast_digest") or is_extractive_fast_judgment_digest(summary, title):
        return "fast_extractive"
    if ("dr-lawbot.com" in url or "tlr." in url) and len(summary) < 280:
        return "fast_extractive"
    if not summary:
        return "empty_summary"
    return ""


def _is_high_quality_judgment_item(item: Dict[str, Any]) -> bool:
    return _judgment_item_quality_issue(item) == ""


def _high_quality_judgment_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in items if _is_high_quality_judgment_item(item)]


def _judgment_quality_rejection_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"fast_extractive": 0, "degraded": 0, "empty_summary": 0}
    for item in items:
        issue = _judgment_item_quality_issue(item)
        if issue:
            counts[issue] = counts.get(issue, 0) + 1
    return counts


def _high_quality_judgment_count(payload: Dict[str, Any]) -> int:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    return len(_high_quality_judgment_items(items))


def _payload_with_high_quality_judgments(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"success": False, "error": "invalid_judgment_payload"}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        return payload
    quality_items = _high_quality_judgment_items(items)
    counts = _judgment_quality_rejection_counts(items)
    out = {
        **payload,
        "items": quality_items,
        "rejected_fast_digest_count": counts.get("fast_extractive", 0),
        "rejected_degraded_count": counts.get("degraded", 0),
        "rejected_empty_summary_count": counts.get("empty_summary", 0),
    }
    if payload.get("success") and not quality_items:
        out["success"] = False
        out["error"] = "no_high_quality_judgment_matches"
    return out


def _run_skill_json(skill_script: str, task: str, timeout_sec: int) -> Dict[str, Any]:
    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    proc = subprocess.run(
        [py, skill_script, "--task", task],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        cwd=_MAGI_ROOT,
        env=os.environ.copy(),
    )
    out = (proc.stdout or "").strip()
    err_text = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": (err_text or out or "unknown")[:280], "returncode": proc.returncode}
    if not out:
        return {"ok": False, "error": "empty_output", "returncode": proc.returncode}
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"ok": False, "error": out[:500], "returncode": proc.returncode}


def _is_practical_insight_request(message: str) -> bool:
    text = str(message or "")
    return any(keyword in text for keyword in ["實務見解", "法律見解", "法院見解"])


def _is_legal_research_request(message: str) -> bool:
    text = str(message or "")
    if _is_practical_insight_request(text):
        return True
    return any(
        keyword in text
        for keyword in [
            "查判決",
            "找判決",
            "判決搜尋",
            "搜尋判決",
            "收集判決",
            "判決搜集",
            "搜尋最高法院判決",
            "查裁判",
            "找裁判",
            "裁判搜尋",
            "搜尋裁判",
            "查法院",
            "法院判決",
            "最高法院",
            "最高行政法院",
            "大法庭",
            "查法規",
            "查法條",
            "法規查詢",
            "法條查詢",
            "釋字",
            "憲判",
        ]
    )


def _with_legal_workflow_footer(reply: str, query: str, *, tool_used: bool = True) -> str:
    workflow = detect_legal_workflow(text=query, mode="legal")
    return append_workflow_footer(reply, workflow, tool_used=tool_used)


def _mcp_lookup_allowed() -> bool:
    return taiwan_legal_mcp_enabled() and taiwan_legal_mcp_available()


def _augment_judgments_with_mcp(
    query: str,
    judgments: Dict[str, Any],
    *,
    case_type: str = "",
    limit: int = 3,
) -> Dict[str, Any]:
    if not _mcp_lookup_allowed():
        return judgments
    primary = _payload_with_high_quality_judgments(judgments)
    mcp_judgments = search_practical_judgments_via_mcp(
        query,
        case_type=case_type,
        limit=int(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_MAX_RESULTS", str(limit)) or str(limit)),
        fulltext_limit=int(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_FULLTEXT_LIMIT", "1") or "1"),
    )
    if mcp_judgments.get("success"):
        return merge_judgment_sources(primary, _payload_with_high_quality_judgments(mcp_judgments), limit=limit)
    if not primary.get("success"):
        return mcp_judgments
    return primary


def _tlr_lookup_allowed() -> bool:
    value = str(os.environ.get("MAGI_TWLEGALRAG_AUGMENT", "1")).strip().lower()
    return tw_legal_rag_enabled() and value not in {"0", "false", "no", "off"}


def _twlegalrag_cache_enabled() -> bool:
    value = str(os.environ.get("MAGI_TWLEGALRAG_CACHE_HITS", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _normalize_tlr_judgment_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    match = re.search(r"(\d{2,3})[./年-](\d{1,2})[./月-](\d{1,2})", text)
    if not match:
        return None
    try:
        year = int(match.group(1))
        if year < 1911:
            year += 1911
        month = int(match.group(2))
        day = int(match.group(3))
        if 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    except Exception:
        return None
    return None


def _cache_tlr_judgments_to_local(tlr_judgments: Dict[str, Any]) -> int:
    """Persist verified TLR hits as a small local cache for repeated queries."""
    if not _twlegalrag_cache_enabled():
        return 0
    items = tlr_judgments.get("items") if isinstance(tlr_judgments.get("items"), list) else []
    if not items:
        return 0
    try:
        db = _get_local_db_manager()
    except Exception as exc:
        logger.debug("tw-legal-rag local cache db unavailable: %s", exc, exc_info=True)
        return 0
    if db is None:
        return 0

    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS court_judgments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                jid VARCHAR(100) UNIQUE,
                court_name VARCHAR(100) DEFAULT NULL,
                case_number VARCHAR(200) DEFAULT NULL,
                case_type VARCHAR(50) DEFAULT NULL,
                judgment_date DATE DEFAULT NULL,
                summary MEDIUMTEXT,
                full_text LONGTEXT,
                source_url TEXT,
                crawled_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    except Exception as exc:
        logger.debug("tw-legal-rag cache table ensure failed: %s", exc, exc_info=True)
        return 0

    cached = 0
    for row in items:
        if not isinstance(row, dict):
            continue
        if not _is_high_quality_judgment_item(row):
            continue
        jid = str(row.get("jid") or row.get("doc_id") or "").strip()[:100]
        if not jid:
            continue
        title = str(row.get("citation_text") or row.get("title") or "").strip()
        summary = str(row.get("summary_full") or row.get("summary_preview") or "").strip()
        if not summary:
            continue
        try:
            db.execute(
                """
                INSERT INTO court_judgments
                    (jid, court_name, case_number, case_type, judgment_date, summary, full_text, source_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    summary = IF(COALESCE(summary, '') = '', VALUES(summary), summary),
                    full_text = IF(COALESCE(full_text, '') = '', VALUES(full_text), full_text),
                    source_url = IF(COALESCE(source_url, '') = '', VALUES(source_url), source_url),
                    crawled_at = CURRENT_TIMESTAMP
                """,
                (
                    jid,
                    str(row.get("court_name") or "").strip() or None,
                    title or jid,
                    str(row.get("case_category") or "一般").strip() or "一般",
                    _normalize_tlr_judgment_date(row.get("judgment_date")),
                    summary[:12000],
                    summary,
                    str(row.get("url") or "").strip(),
                ),
            )
            cached += 1
        except Exception as exc:
            logger.debug("tw-legal-rag cache upsert failed for %s: %s", jid, exc, exc_info=True)
    return cached


def _augment_judgments_with_tlr(
    query: str,
    judgments: Dict[str, Any],
    *,
    limit: int = 3,
) -> Dict[str, Any]:
    if not _tlr_lookup_allowed():
        return judgments
    primary = _payload_with_high_quality_judgments(judgments)
    try:
        tlr_judgments = search_practical_judgments_via_tlr(
            query,
            limit=int(os.environ.get("MAGI_TWLEGALRAG_MAX_RESULTS", str(limit)) or str(limit)),
            fulltext_limit=int(os.environ.get("MAGI_TWLEGALRAG_FULLTEXT_LIMIT", str(limit)) or str(limit)),
        )
    except Exception as exc:
        logger.debug("tw-legal-rag augment failed: %s", exc, exc_info=True)
        return primary
    if tlr_judgments.get("success"):
        cached_count = _cache_tlr_judgments_to_local(tlr_judgments)
        if cached_count:
            tlr_judgments["local_cache_upserts"] = cached_count
        return merge_judgment_sources(primary, _payload_with_high_quality_judgments(tlr_judgments), limit=limit)
    if not primary.get("success"):
        return tlr_judgments
    return primary


def _augment_judgments_with_external_sources(
    query: str,
    judgments: Dict[str, Any],
    *,
    case_type: str = "",
    limit: int = 3,
) -> Dict[str, Any]:
    """Add optional public legal retrieval sources without breaking local results."""
    augmented = judgments
    if _mcp_lookup_allowed():
        augmented = _augment_judgments_with_mcp(query, augmented, case_type=case_type, limit=limit)
    augmented = _augment_judgments_with_tlr(query, augmented, limit=limit)
    return augmented


def _extract_regulation_query(message: str) -> Tuple[str, str]:
    text = re.sub(r"^@MAGI\s*", "", str(message or ""), flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:幫我|請|麻煩|幫忙|可以幫我|協助我)\s*", "", text).strip()
    text = re.sub(r"^(?:查法規|查法條|法規查詢|法條查詢|查詢法規|查詢法條)\s*", "", text).strip(" ：:，,。；;")
    match = re.search(r"(?P<law>[\u4e00-\u9fff]{1,24}?)(?:第)?\s*(?P<article>\d+(?:-\d+)?)\s*條", text)
    if match:
        return match.group("law").strip(), match.group("article").strip()
    return text.strip(), ""


def _format_regulation_mcp_result(query: str, result: Dict[str, Any]) -> str:
    if not result.get("success") and not result.get("ok"):
        return f"❌ 查不到法規資料：{result.get('error') or query}"
    law = result.get("law") if isinstance(result.get("law"), dict) else {}
    law_name = str(law.get("name") or query or "法規").strip()
    lines = [f"📘 法規查詢：{law_name}", "來源：台灣法律資料庫 MCP（全國法規資料庫）"]
    for article in (result.get("articles") or [])[:5]:
        if not isinstance(article, dict):
            continue
        number = str(article.get("article_no") or article.get("number") or "").strip()
        content = str(article.get("content") or article.get("text") or "").strip()
        if number:
            lines.append(f"\n【第 {number} 條】")
        if content:
            lines.append(content[:900])
    source_url = str(result.get("source_url") or "").strip()
    if source_url:
        lines.append(f"\n{source_url}")
    return "\n".join(lines)


def _format_interpretation_mcp_result(query: str, result: Dict[str, Any]) -> str:
    if not result.get("success") and not result.get("ok"):
        return f"❌ 查不到釋憲/憲法法庭資料：{result.get('error') or query}"
    title = str(result.get("case_id") or result.get("number") or result.get("title") or query).strip()
    lines = [f"⚖️ 釋憲／憲法法庭查詢：{title}", "來源：台灣法律資料庫 MCP（憲法法庭公開資料）"]
    for key in ["date", "issue", "holding", "summary", "explanation", "main_text"]:
        value = str(result.get(key) or "").strip()
        if value:
            lines.append(f"\n【{key}】\n{value[:900]}")
    source_url = str(result.get("source_url") or "").strip()
    if source_url:
        lines.append(f"\n{source_url}")
    return "\n".join(lines)


def _run_direct_taiwan_legal_mcp_lookup(message: str) -> str:
    if not _mcp_lookup_allowed():
        return ""
    text = str(message or "")
    if any(k in text for k in ["查法規", "查法條", "法規查詢", "法條查詢", "查詢法規", "查詢法條"]):
        law_name, article_no = _extract_regulation_query(text)
        if not law_name:
            return "🔎 請提供法規名稱或條號，例如：`查法條 民法第184條`。"
        result = call_taiwan_legal_tool("query_regulation", law_name=law_name, article_no=article_no)
        return _format_regulation_mcp_result(" ".join(part for part in [law_name, article_no] if part), result)
    if "釋字" in text or "憲判" in text:
        cleaned = re.sub(r"^(?:查|查詢|找|搜尋)\s*", "", text).strip(" ：:，,。；;")
        result = call_taiwan_legal_tool("get_interpretation", case_id=cleaned)
        return _format_interpretation_mcp_result(cleaned, result)
    return ""


def _format_statute_items(items: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for item in items[:3]:
        source = str(item.get("source") or "")
        match = re.search(r"law=([^|]+)\|article=([^|]+)", source)
        law = match.group(1) if match else ""
        article = match.group(2) if match else ""
        content = str(item.get("content") or "").strip().replace("\n", " ")
        if len(content) > 120:
            content = content[:120] + "…"
        label = " ".join(part for part in [law, article] if part)
        lines.append(f"- {label or source}: {content}")
    return lines


_INSIGHT_STOPWORDS = {
    "實務見解",
    "法律見解",
    "法院見解",
    "判決",
    "裁判",
    "最高法院",
    "高等法院",
    "地方法院",
    "查詢",
    "整理",
    "關於",
}

_LEGAL_REASONING_MARKERS = (
    "本院認為",
    "本院判斷",
    "法院認為",
    "最高法院",
    "按",
    "惟",
    "查",
    "經查",
    "又查",
    "又",
    "次按",
    "準此",
    "是以",
    "足認",
    "應認",
    "堪認",
    "尚難",
    "不得",
    "應依",
    "有理由",
    "無理由",
)

_LOW_VALUE_INSIGHT_MARKERS = (
    "主文",
    "事實及理由",
    "程序事項",
    "案由",
    "當事人",
    "上列",
    "本件",
    "目錄",
    "全文",
)


def _insight_query_terms(query: str) -> List[str]:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(query or ""))
    terms: List[str] = []
    for raw in text.split():
        token = raw.strip()
        if len(token) < 2 or token in _INSIGHT_STOPWORDS:
            continue
        terms.append(token)
    # Add overlapping Chinese chunks for short legal phrases so matching works
    # when the DB text has no spaces.
    expanded: List[str] = []
    for token in terms:
        expanded.append(token)
        if re.search(r"[\u4e00-\u9fff]", token) and len(token) >= 4:
            expanded.extend(token[i : i + 2] for i in range(len(token) - 1))
    deduped: List[str] = []
    for token in expanded:
        if token not in deduped and token not in _INSIGHT_STOPWORDS:
            deduped.append(token)
    return deduped[:12]


def _insight_primary_terms(query: str) -> List[str]:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(query or ""))
    terms: List[str] = []
    for raw in text.split():
        token = raw.strip()
        if len(token) < 2 or token in _INSIGHT_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:8]


def _insight_unit_matches_query(unit: str, primary_terms: List[str]) -> bool:
    """Reject readable but unrelated passages from broad judgment matches."""
    if not primary_terms:
        return True
    compact = re.sub(r"\s+", "", unit)
    for term in primary_terms:
        if term in compact:
            return True
        if re.search(r"[\u4e00-\u9fff]", term) and len(term) >= 4:
            chunks = [term[i : i + 2] for i in range(len(term) - 1)]
            hits = {chunk for chunk in chunks if chunk and chunk in compact}
            if len(hits) >= 2:
                return True
    return False


def _clean_insight_text(text: object) -> str:
    value = str(text or "")
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _split_insight_units(text: str) -> List[str]:
    cleaned = _clean_insight_text(text)
    if not cleaned:
        return []
    rough_parts = re.split(r"\n{2,}|(?<=。)\s*(?=(?:按|惟|查|經查|又|次按|本院|法院|準此|是以))", cleaned)
    units: List[str] = []
    for part in rough_parts:
        part = re.sub(r"\s+", " ", part).strip(" ，,。；;\n\t")
        if len(part) < 28:
            continue
        if is_extractive_fast_judgment_digest(part):
            continue
        units.append(part)
    return units


def _score_insight_unit(unit: str, terms: List[str]) -> int:
    compact = re.sub(r"\s+", "", unit)
    if not compact:
        return -999
    score = 0
    for term in terms:
        if term and term in compact:
            score += 18 if len(term) >= 3 else 8
    for marker in _LEGAL_REASONING_MARKERS:
        if marker in compact:
            score += 16
    if re.search(r"第\s*\d+\s*條|民法|刑法|消費者債務清理條例|公司法|民事訴訟法|刑事訴訟法", compact):
        score += 18
    if re.search(r"\d{2,3}\s*年度.{0,8}字第?\s*\d+\s*號|最高法院.{0,20}判決", compact):
        score += 10
    if len(compact) > 120:
        score += 8
    if len(compact) > 520:
        score -= 10
    if any(marker in compact[:80] for marker in _LOW_VALUE_INSIGHT_MARKERS):
        score -= 8
    if "無可擷取" in compact or "請提供" in compact:
        score -= 80
    return score


def _find_focus_offset(text: str, focus_terms: List[str]) -> int:
    for term in focus_terms:
        if not term:
            continue
        idx = text.find(term)
        if idx >= 0:
            return idx
        if re.search(r"[\u4e00-\u9fff]", term):
            pattern = r"\s*".join(re.escape(ch) for ch in term)
            match = re.search(pattern, text)
            if match:
                return match.start()
    return -1


def _trim_insight_unit(unit: str, max_chars: int = 360, focus_terms: Optional[List[str]] = None) -> str:
    text = re.sub(r"\s+", " ", unit).strip(" ，,。；;")
    if len(text) <= max_chars:
        return text
    focus_terms = focus_terms or []
    focus_at = _find_focus_offset(text, focus_terms)
    if focus_at > max_chars:
        start = max(0, focus_at - max_chars // 3)
        boundary = max(text.rfind("。", 0, start), text.rfind("；", 0, start), text.rfind("，", 0, start))
        if boundary >= max(0, start - 80):
            start = boundary + 1
        window = text[start : start + max_chars]
        end_boundary = max(window.rfind("。"), window.rfind("；"), window.rfind(";"))
        if end_boundary >= 120:
            window = window[: end_boundary + 1]
        return ("…" if start > 0 else "") + window.strip(" ，,。；;") + ("…" if start + len(window) < len(text) else "")
    cut = text[:max_chars]
    last_stop = max(cut.rfind("。"), cut.rfind("；"), cut.rfind(";"))
    if last_stop >= 120:
        return cut[: last_stop + 1]
    return cut.rstrip(" ，,。；;") + "…"


def _extract_practical_insight_excerpt(item: Dict[str, Any], query: str, max_chars: int = 360) -> str:
    """Return the most readable, query-relevant legal reasoning passage."""
    terms = _insight_query_terms(query)
    primary_terms = _insight_primary_terms(query)
    sources = [
        item.get("summary_full"),
        item.get("summary_preview"),
        item.get("summary"),
        item.get("insight_text"),
        item.get("full_text_excerpt"),
        item.get("full_text"),
        item.get("raw_text"),
    ]
    candidates: List[Tuple[int, str]] = []
    for source in sources:
        for unit in _split_insight_units(str(source or "")):
            if not _insight_unit_matches_query(unit, primary_terms):
                continue
            candidates.append((_score_insight_unit(unit, terms), unit))
    candidates = [(score, unit) for score, unit in candidates if score > -20]
    if not candidates:
        if not primary_terms:
            fallback = _clean_insight_text(next((str(s or "") for s in sources if str(s or "").strip()), ""))
            return _trim_insight_unit(fallback, max_chars=max_chars) if fallback else ""
        return ""
    candidates.sort(key=lambda pair: (pair[0], len(pair[1])), reverse=True)
    return _trim_insight_unit(candidates[0][1], max_chars=max_chars, focus_terms=primary_terms)


def _query_relevant_judgment_items(items: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    return [item for item in items if _extract_practical_insight_excerpt(item, query, max_chars=120)]


def _search_local_judgment_archive(query: str, limit: int = 3) -> Dict[str, Any]:
    """本地實務見解庫 fallback（判決-搜尋）。

    2026-04-21 後：主查詢來源改為 `court_judgments`（與 OSC 查詢頁同一張表）。
    `judgment_archive` 已由 `scripts/ops/merge_judgment_archive_to_court.py` 合併
    至 `court_judgments`；仍留的 662 筆無 jid 舊案作為 secondary fallback。
    """
    text = str(query or "").strip()
    if not text:
        return {"success": False, "error": "missing_query"}
    try:
        db = _get_local_db_manager()
        if db is None:
            return {"success": False, "error": "local_archive_db_unavailable"}
        like = f"%{text}%"
        limit_int = max(1, int(limit))

        # 主查詢：court_judgments（OSC 可見正式實務見解庫）
        rows = db.execute(
            """
            SELECT
                jid,
                court_name,
                case_number,
                case_type,
                judgment_date,
                LEFT(COALESCE(summary, ''), 1200) AS summary_text,
                CASE
                    WHEN LOCATE(%s, COALESCE(full_text, '')) > 0
                    THEN SUBSTRING(COALESCE(full_text, ''), GREATEST(LOCATE(%s, COALESCE(full_text, '')) - 700, 1), 2400)
                    ELSE LEFT(COALESCE(full_text, ''), 2400)
                END AS full_text_excerpt,
                source_url,
                crawled_at
            FROM court_judgments
            WHERE
                case_number LIKE %s
                OR case_type LIKE %s
                OR summary LIKE %s
                OR full_text LIKE %s
                OR court_name LIKE %s
            ORDER BY
                CASE
                    WHEN case_number LIKE %s THEN 0
                    WHEN case_type LIKE %s THEN 1
                    WHEN summary LIKE %s THEN 2
                    WHEN full_text LIKE %s THEN 3
                    ELSE 4
                END,
                crawled_at DESC
            LIMIT %s
            """,
            (text, text, like, like, like, like, like, like, like, like, like, limit_int),
            fetch="all",
        ) or []
    except Exception as exc:
        logger.warning("local judgment court_judgments fallback failed: %s", exc)
        return {"success": False, "error": f"local_archive_failed: {str(exc)[:160]}"}

    items: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        summary = str(row.get("summary_text") or "").strip()
        is_degraded = "系統降級回覆" in summary
        is_fast_digest = is_extractive_fast_judgment_digest(summary)
        court_name = str(row.get("court_name") or "").strip()
        case_number = str(row.get("case_number") or "").strip()
        title_parts = [p for p in [court_name, case_number] if p]
        title = " ".join(title_parts) if title_parts else str(row.get("jid") or "").strip()
        items.append(
            {
                "title": title,
                "summary_preview": summary,
                "full_text_excerpt": str(row.get("full_text_excerpt") or "").strip(),
                "court_name": court_name,
                "case_number": case_number,
                "case_type": str(row.get("case_type") or "").strip(),
                "judgment_date": str(row.get("judgment_date") or "").strip(),
                "url": str(row.get("source_url") or "").strip(),
                "is_degraded": is_degraded,
                "is_fast_digest": is_fast_digest,
                "source": "court_judgments_local",
            }
        )

    # Secondary fallback: 662 筆無 jid 舊 judgment_archive（merge 時無法正規化）
    if not items:
        try:
            db = _get_local_db_manager()
            if db is not None:
                like = f"%{text}%"
                legacy_rows = db.execute(
                    """
                    SELECT
                        judgment_title,
                        judgment_url,
                        LEFT(summary_text, 1200) AS summary_text,
                        case_reason,
                        crawled_at
                    FROM judgment_archive
                    WHERE
                        (source_jid IS NULL OR source_jid = '')
                        AND (
                            case_reason LIKE %s
                            OR summary_text LIKE %s
                            OR judgment_title LIKE %s
                        )
                    ORDER BY crawled_at DESC
                    LIMIT %s
                    """,
                    (like, like, like, max(1, int(limit))),
                    fetch="all",
                ) or []
                for row in legacy_rows:
                    if not isinstance(row, dict):
                        continue
                    summary = str(row.get("summary_text") or "").strip()
                    is_degraded = "系統降級回覆" in summary
                    is_fast_digest = is_extractive_fast_judgment_digest(summary)
                    items.append(
                        {
                            "title": str(row.get("judgment_title") or "").strip(),
                            "summary_preview": summary,
                            "url": str(row.get("judgment_url") or "").strip(),
                            "is_degraded": is_degraded,
                            "is_fast_digest": is_fast_digest,
                            "source": "judgment_archive_legacy",
                        }
                    )
        except Exception as exc:
            logger.debug("legacy judgment_archive secondary fallback failed: %s", exc)

    items = [item for item in items if item.get("title")]
    rejection_counts = _judgment_quality_rejection_counts(items)
    quality_items = _high_quality_judgment_items(items)[: max(1, int(limit))]
    if not quality_items:
        if items:
            return {
                "success": False,
                "error": "no_high_quality_local_archive_matches",
                "source_label": "本地實務見解庫",
                "rejected_fast_digest_count": rejection_counts.get("fast_extractive", 0),
                "rejected_degraded_count": rejection_counts.get("degraded", 0),
                "rejected_empty_summary_count": rejection_counts.get("empty_summary", 0),
            }
        return {"success": False, "error": "no_local_archive_matches"}
    quality_items = _query_relevant_judgment_items(quality_items, text)
    if not quality_items:
        return {
            "success": False,
            "error": "no_query_relevant_local_archive_matches",
            "source_label": "本地實務見解庫",
            "rejected_fast_digest_count": rejection_counts.get("fast_extractive", 0),
            "rejected_degraded_count": rejection_counts.get("degraded", 0),
            "rejected_empty_summary_count": rejection_counts.get("empty_summary", 0),
        }
    return {
        "success": True,
        "source_label": "本地實務見解庫",
        "items": quality_items,
        "rejected_fast_digest_count": rejection_counts.get("fast_extractive", 0),
        "rejected_degraded_count": rejection_counts.get("degraded", 0),
        "rejected_empty_summary_count": rejection_counts.get("empty_summary", 0),
    }


def format_practical_insight_result(query: str, judgments: Dict[str, Any], statutes: Dict[str, Any]) -> str:
    lines = [f"📚 實務見解整理：{query}"]

    statute_items = statutes.get("items") if isinstance(statutes.get("items"), list) else []
    if statute_items:
        lines.append("\n【適用法規】")
        lines.extend(_format_statute_items(statute_items))
    elif statutes.get("error"):
        lines.append(f"\n【適用法規】\n- 查詢失敗：{statutes.get('error')}")

    if judgments.get("success"):
        source_label = str(judgments.get("source_label") or "").strip()
        if source_label:
            lines.append(f"\n【相關判決／法院見解】（{source_label}）")
        else:
            lines.append("\n【相關判決／法院見解】")
        raw_items = judgments.get("items") if isinstance(judgments.get("items"), list) else []
        items = _high_quality_judgment_items(raw_items)
        relevant_items = _query_relevant_judgment_items(items, query)
        reject_counts = _judgment_quality_rejection_counts(raw_items)
        rejected_total = sum(reject_counts.values())
        if rejected_total:
            lines.append(
                "- 已排除低品質候選："
                f"抽取式快篩 {reject_counts.get('fast_extractive', 0)}、"
                f"降級摘要 {reject_counts.get('degraded', 0)}、"
                f"缺摘要 {reject_counts.get('empty_summary', 0)}。"
            )
        if raw_items and not relevant_items:
            lines.append("- 已找到候選裁判，但目前沒有可讀且命中查詢重點的法院見解；MAGI 已阻擋其作為正式實務見解。")
            lines.append("- 請改用全文/TLR 精查，或稍後讓夜間重摘要補齊後再引用。")
            return "\n".join(lines)
        for row in relevant_items[:3]:
            title = str(row.get("title") or "").strip()
            summary = _extract_practical_insight_excerpt(row, query, max_chars=420)
            if title:
                lines.append(f"- {title}")
            if summary:
                lines.append(f"  核心見解：{summary}")
            url = str(row.get("url") or "").strip()
            if url:
                lines.append(f"  {url}")
    else:
        error = str(judgments.get("error") or "unknown")
        if error in {
            "no_high_quality_judgment_matches",
            "no_high_quality_local_archive_matches",
            "no_query_relevant_local_archive_matches",
        }:
            lines.append("\n【相關判決／法院見解】")
            lines.append("- 已找到候選裁判，但目前沒有可讀且命中查詢重點的法院見解；MAGI 已阻擋其作為正式實務見解。")
            lines.append("- 請改用全文/TLR 精查，或稍後讓夜間重摘要補齊後再引用。")
        else:
            lines.append(f"\n【相關判決／法院見解】\n- 查詢失敗：{judgments.get('error') or 'unknown'}")
    return "\n".join(lines)


def run_practical_insight_command(orch, message: str, notify: bool = False) -> str:
    payload, err = extract_judgment_collect_payload(message)
    if not payload:
        return err

    query = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
    judgment_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
    statutes_script = f"{_MAGI_ROOT}/skills/statutes-vdb/action.py"
    if not os.path.exists(judgment_script):
        return "❌ 找不到實務見解判決來源。"
    if not os.path.exists(statutes_script):
        return "❌ 找不到法規查詢來源。"

    judgment_payload = {
        **payload,
        "max_results": int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "6") or "6"),
        "headless": True,
        "save_to_db": True,
        "notify": bool(notify),
    }
    judgments = _run_skill_json(
        judgment_script,
        "collect " + json.dumps(judgment_payload, ensure_ascii=False),
        timeout_sec=int(os.environ.get("MAGI_JUDGMENT_CHAT_TIMEOUT_SEC", "180") or "180"),
    )
    if (not judgments.get("success")) or (not isinstance(judgments.get("items"), list)) or (not judgments.get("items")):
        fallback = _search_local_judgment_archive(query, limit=3)
        if fallback.get("success"):
            judgments = fallback
    primary_quality_count = _high_quality_judgment_count(judgments)
    should_augment = str(os.environ.get("MAGI_LEGAL_EXTERNAL_AUGMENT", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if should_augment or (not judgments.get("success")) or primary_quality_count < 2:
        judgments = _augment_judgments_with_external_sources(
            query,
            judgments,
            case_type=str(payload.get("case_type") or ""),
            limit=int(os.environ.get("MAGI_LEGAL_EXTERNAL_MAX_RESULTS", "3") or "3"),
        )
    statutes = _run_skill_json(
        statutes_script,
        "search " + json.dumps({"query": query, "top_k": 5}, ensure_ascii=False),
        timeout_sec=int(os.environ.get("MAGI_STATUTE_CHAT_TIMEOUT_SEC", "90") or "90"),
    )
    return _with_legal_workflow_footer(format_practical_insight_result(query, judgments, statutes), query, tool_used=True)


def run_judgment_collector_command(orch, message: str, notify: bool = False) -> str:
    if _is_practical_insight_request(message):
        return run_practical_insight_command(orch, message, notify=notify)
    direct = _run_direct_taiwan_legal_mcp_lookup(message)
    if direct:
        return _with_legal_workflow_footer(direct, message, tool_used=True)
    payload, err = extract_judgment_collect_payload(message)
    if not payload:
        return err

    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    skill_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
    if not os.path.exists(skill_script):
        return "\u274c \u627e\u4e0d\u5230\u5224\u6c7a\u641c\u5c0b skill\u3002"

    payload = {
        **payload,
        "max_results": int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
        "headless": True,
        "save_to_db": True,
        "notify": bool(notify),
    }
    task = "collect " + json.dumps(payload, ensure_ascii=False)
    try:
        data = _run_skill_json(
            skill_script,
            task,
            timeout_sec=int(os.environ.get("MAGI_JUDGMENT_CHAT_TIMEOUT_SEC", "180") or "180"),
        )
    except Exception as e:
        return f"\u274c \u5224\u6c7a\u641c\u5c0b\u932f\u8aa4\uff1a{e}"
    if not isinstance(data, dict):
        return str(data)[:1500]
    if not data.get("success"):
        query = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
        fallback = _search_local_judgment_archive(
            query,
            limit=int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
        )
        if fallback.get("success"):
            fallback = _augment_judgments_with_external_sources(
                query,
                fallback,
                limit=int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
            )
            return _with_legal_workflow_footer(format_judgment_collect_result({
                "success": True,
                "case_reason": query,
                "count": len(fallback.get("items") or []),
                "items": fallback.get("items") or [],
                "source_label": fallback.get("source_label", "本地實務見解庫"),
            }), query, tool_used=True)
        mcp_fallback = _augment_judgments_with_external_sources(
            query,
            {"success": False, "error": str(data.get("error") or "collector_failed")},
            limit=int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
        )
        if mcp_fallback.get("success"):
            return _with_legal_workflow_footer(format_judgment_collect_result({
                "success": True,
                "case_reason": query,
                "count": len(mcp_fallback.get("items") or []),
                "items": mcp_fallback.get("items") or [],
                "source_label": mcp_fallback.get("source_label", "外部判決公開資料"),
            }), query, tool_used=True)
        return f"\u274c \u5224\u6c7a\u641c\u5c0b\u5931\u6557\uff1a{str(data.get('error') or 'unknown')[:280]}"
    query = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
    data = _augment_judgments_with_external_sources(
        query,
        data,
        limit=int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
    )
    return _with_legal_workflow_footer(format_judgment_collect_result(data), query, tool_used=True)


def run_judgment_trend_command(orch, message: str) -> str:
    """Execute judgment trend analysis."""
    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    skill_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
    if not os.path.exists(skill_script):
        return "\u274c \u627e\u4e0d\u5230\u5224\u6c7a\u641c\u5c0b skill\u3002"
    case_reason = ""
    for prefix in ["\u5224\u6c7a\u8da8\u52e2", "\u8da8\u52e2\u5206\u6790", "\u6848\u7531\u5206\u6790", "\u5224\u6c7a\u5206\u6790"]:
        if prefix in message:
            case_reason = message.split(prefix)[-1].strip()
            break
    payload = {}
    if case_reason:
        payload["case_reason"] = case_reason
    task = "trend_analysis " + json.dumps(payload, ensure_ascii=False) if payload else "trend_analysis"
    try:
        proc = subprocess.run(
            [py, skill_script, "--task", task],
            capture_output=True, text=True, timeout=30,
            cwd=_MAGI_ROOT, env=os.environ.copy(),
        )
        return (proc.stdout or "").strip()[:2000] or "\u274c \u8da8\u52e2\u5206\u6790\u7121\u8f38\u51fa"
    except Exception as e:
        return f"\u274c \u8da8\u52e2\u5206\u6790\u932f\u8aa4\uff1a{e}"
