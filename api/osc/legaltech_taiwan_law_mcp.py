"""Privacy-bounded client for https://legaltech.org.tw/mcp.

The remote service is used only for public legal research.  Callers must pass
the result of :func:`prepare_external_legal_query`; this module deliberately
does not accept case folders, client records, or arbitrary MCP endpoints.
Remote judgments remain discovery candidates until MAGI binds the JID/case
number to an official local full text and the normal pleading quality gate.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any, Dict, List


DEFAULT_ENDPOINT = "https://legaltech.org.tw/mcp"
SOURCE = "legaltech_taiwan_law_mcp"
SOURCE_LABEL = "Taiwan Law MCP（法務部／司法院／憲法法庭／立法院公開資料）"
_MAX_RESPONSE_BYTES = 3 * 1024 * 1024
_ALLOWED_TOOLS = {
    "analyze_legal_intent",
    "search_taiwan_laws",
    "search_taiwan_judgments",
    "get_taiwan_judgment",
    "search_taiwan_regulations",
    "get_taiwan_pcode",
    "get_taiwan_interpretation",
    "search_taiwan_interpretations",
    "get_taiwan_interpretation_citations",
    "get_taiwan_law_progress",
    "get_taiwan_law_versions",
    "search_taiwan_bills",
    "search_moj_interpretations",
    "get_moj_draft_announcements",
}

# This is deliberately a fixed catalogue rather than provider discovery.  It
# gives every MAGI channel the same bounded public-research capability without
# allowing a conversational prompt to select an arbitrary remote tool.
LEGALTECH_TOOL_CATALOG: tuple[dict[str, str], ...] = (
    {"name": "analyze_legal_intent", "kind": "route", "scope": "白話法律問題分類"},
    {"name": "search_taiwan_laws", "kind": "law", "scope": "全國法規與條文"},
    {"name": "search_taiwan_judgments", "kind": "judgment", "scope": "司法院裁判候選"},
    {"name": "get_taiwan_judgment", "kind": "judgment", "scope": "單筆裁判全文"},
    {"name": "search_taiwan_regulations", "kind": "law", "scope": "法規搜尋"},
    {"name": "get_taiwan_pcode", "kind": "law", "scope": "法規條文"},
    {"name": "get_taiwan_interpretation", "kind": "interpretation", "scope": "釋憲與憲法法庭"},
    {"name": "search_taiwan_interpretations", "kind": "interpretation", "scope": "釋憲搜尋"},
    {"name": "get_taiwan_interpretation_citations", "kind": "interpretation", "scope": "釋憲引用關聯"},
    {"name": "get_taiwan_law_progress", "kind": "legislation", "scope": "立法歷程"},
    {"name": "get_taiwan_law_versions", "kind": "legislation", "scope": "法規沿革"},
    {"name": "search_taiwan_bills", "kind": "legislation", "scope": "法案搜尋"},
    {"name": "search_moj_interpretations", "kind": "interpretation", "scope": "法務部函釋"},
    {"name": "get_moj_draft_announcements", "kind": "legislation", "scope": "草案公告"},
)
_OFFICIAL_HOST_SUFFIXES = (
    "judicial.gov.tw", "moj.gov.tw", "ly.gov.tw", "cons.judicial.gov.tw",
)


def legaltech_mcp_enabled() -> bool:
    return str(os.environ.get("MAGI_LEGALTECH_TAIWAN_LAW_MCP_ENABLE", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _endpoint() -> str:
    value = str(os.environ.get("MAGI_LEGALTECH_TAIWAN_LAW_MCP_URL") or DEFAULT_ENDPOINT).strip()
    parsed = urllib.parse.urlparse(value)
    allowed = {
        host.strip().lower()
        for host in str(
            os.environ.get("MAGI_LEGALTECH_TAIWAN_LAW_MCP_ALLOWED_HOSTS") or "legaltech.org.tw"
        ).split(",")
        if host.strip()
    }
    if parsed.scheme != "https" or parsed.hostname is None or parsed.hostname.lower() not in allowed:
        raise ValueError("legaltech_mcp_endpoint_not_allowed")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("legaltech_mcp_endpoint_not_allowed")
    return value


def _timeout() -> float:
    try:
        return max(3.0, min(30.0, float(os.environ.get("MAGI_LEGALTECH_TAIWAN_LAW_MCP_TIMEOUT_SEC", "15"))))
    except (TypeError, ValueError):
        return 15.0


def _decode_json_or_sse(body: bytes, content_type: str) -> Dict[str, Any]:
    text = body.decode("utf-8", errors="strict")
    if "text/event-stream" in str(content_type or "").lower() or text.lstrip().startswith("event:"):
        payloads = []
        for line in text.splitlines():
            if line.startswith("data:"):
                value = line[5:].strip()
                if value and value != "[DONE]":
                    payloads.append(value)
        if not payloads:
            raise ValueError("legaltech_mcp_empty_sse")
        text = payloads[-1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("legaltech_mcp_invalid_response")
    return value


def _post_jsonrpc(payload: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = _endpoint()
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "MAGI-V3 Taiwan-Law-MCP/1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_timeout(), context=ssl.create_default_context()) as response:
        final = urllib.parse.urlparse(response.geturl())
        expected = urllib.parse.urlparse(endpoint)
        if final.scheme != "https" or final.hostname != expected.hostname:
            raise ValueError("legaltech_mcp_redirect_not_allowed")
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("legaltech_mcp_response_too_large")
        return _decode_json_or_sse(body, response.headers.get("Content-Type", ""))


def _structured_result(envelope: Dict[str, Any]) -> Dict[str, Any]:
    if envelope.get("error"):
        return {"ok": False, "success": False, "error": "remote_mcp_error", "source": SOURCE}
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    if result.get("isError"):
        return {"ok": False, "success": False, "error": "remote_tool_error", "source": SOURCE}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return {**structured, "ok": True, "success": True, "source": SOURCE}
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            parsed = json.loads(str(item.get("text") or ""))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return {**parsed, "ok": True, "success": True, "source": SOURCE}
    return {"ok": False, "success": False, "error": "remote_mcp_unstructured_result", "source": SOURCE}


def _official_urls(value: Any) -> list[str]:
    """Return only inspectable government sources from a provider response."""
    found: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and current.startswith("https://"):
            parsed = urllib.parse.urlparse(current)
            host = str(parsed.hostname or "").lower()
            if any(host == suffix or host.endswith("." + suffix) for suffix in _OFFICIAL_HOST_SUFFIXES):
                if current not in found:
                    found.append(current)
    return found[:12]


def legaltech_tool_catalog() -> list[dict[str, str]]:
    """Public, stable tool metadata for Web/Mobile/Telegram status surfaces."""
    return [dict(item) for item in LEGALTECH_TOOL_CATALOG]


def call_legaltech_tool(tool_name: str, **arguments: Any) -> Dict[str, Any]:
    """Call a fixed public legal-research tool without exposing raw errors."""
    if not legaltech_mcp_enabled():
        return {"ok": False, "success": False, "error": "legaltech_mcp_disabled", "source": SOURCE}
    if tool_name not in _ALLOWED_TOOLS:
        return {"ok": False, "success": False, "error": "unsupported_tool", "source": SOURCE}
    try:
        envelope = _post_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
        result = _structured_result(envelope)
        result["tool"] = tool_name
        result["provider"] = SOURCE_LABEL
        result["contract_version"] = "magi.public-legal-research/v1"
        result["status"] = "ok" if result.get("success") else "unavailable"
        result["official_urls"] = _official_urls(result) if result.get("success") else []
        return result
    except Exception:
        # Network/TLS/provider internals are deliberately not returned to UI.
        return {
            "ok": False,
            "success": False,
            "error": "remote_mcp_unavailable",
            "source": SOURCE,
            "tool": tool_name,
            "provider": SOURCE_LABEL,
            "contract_version": "magi.public-legal-research/v1",
            "status": "unavailable",
            "official_urls": [],
        }


def analyze_legal_intent_via_legaltech(safe_query: str) -> Dict[str, Any]:
    text = str(safe_query or "").strip()
    if len(text) < 3:
        return {"ok": False, "success": False, "error": "missing_safe_query", "source": SOURCE}
    return call_legaltech_tool("analyze_legal_intent", text=text[:12000])


def _case_title(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _judgment_search_query(query: str, analysis: Dict[str, Any]) -> str:
    """Convert a natural-language question into provider search terms.

    The MCP intent tool often removes punctuation but deliberately preserves
    the user's sentence.  The judgment search endpoint is keyword-oriented,
    so common question particles otherwise cause false zero-result replies.
    This normalizer removes only discourse/question words and retains legal
    concepts such as ``舉證責任`` and ``分配``.
    """
    suggestions = analysis.get("suggested_queries") if isinstance(analysis, dict) else []
    # The caller's privacy-scrubbed terms are authoritative.  An intent-model
    # suggestion may be narrower (for example it once reduced
    # "法官＋案由＋量刑" to the judge name alone), so suggestions are fallback
    # material rather than a replacement for explicit filters.
    candidates = [str(query or "").strip()]
    candidates.extend(str(value or "").strip() for value in suggestions or [] if str(value or "").strip())
    text = candidates[0]
    text = re.sub(r"[？?！!。，,；;:：、]+", " ", text)
    text = re.sub(r"^(?:請問|想請問|請教|我想知道|我想了解|法律上|實務上)+", " ", text)
    text = re.sub(r"(?:怎麼|如何|為何|是否|可否|能否|會不會|嗎|嘛|呢)", " ", text)
    text = text.replace("的", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return (text or str(query or "").strip())[:128]


def search_practical_judgments_via_legaltech(
    safe_query: str,
    *,
    court: str = "",
    case_type: str = "",
    limit: int = 3,
    fulltext_limit: int = 1,
) -> Dict[str, Any]:
    """Discover official judgments; never marks them pleading-eligible."""
    query = str(safe_query or "").strip()
    if len(query) < 2:
        return {"ok": False, "success": False, "error": "missing_safe_query", "source": SOURCE}
    analysis = analyze_legal_intent_via_legaltech(query)
    search_query = _judgment_search_query(query, analysis)
    arguments: Dict[str, Any] = {"query": search_query, "limit": max(1, min(10, int(limit)))}
    if str(court or "").strip():
        arguments["court"] = str(court).strip()[:30]
    if case_type in {"民事", "刑事", "行政", "懲戒"}:
        arguments["case_type"] = case_type
    search = call_legaltech_tool("search_taiwan_judgments", **arguments)
    if not search.get("success"):
        return {**search, "intent_analysis": analysis if analysis.get("success") else {}}

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in search.get("results") or []:
        if not isinstance(raw, dict):
            continue
        jid = str(raw.get("jid") or "").strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        detail: Dict[str, Any] = {}
        if len(items) < max(0, min(10, int(fulltext_limit))):
            candidate = call_legaltech_tool("get_taiwan_judgment", jid=jid, fresh=False)
            if candidate.get("success"):
                detail = candidate
        full_text = str(detail.get("content") or "").strip()
        official_url = str(detail.get("official_url") or raw.get("url") or "").strip()
        title = _case_title(detail.get("title") or raw.get("case_id") or jid)
        summary = str(raw.get("summary") or "").strip()
        items.append(
            {
                "title": title,
                "citation_text": title,
                "summary_preview": summary or "已命中司法院公開裁判，引用前須核對全文。",
                "summary_full": summary,
                "full_text": full_text,
                "url": official_url,
                "source_url": official_url,
                "jid": jid,
                "court": str(raw.get("court") or "").strip(),
                "case_reason": str(raw.get("cause") or "").strip(),
                "judgment_date": str(raw.get("date") or "").strip(),
                "source": SOURCE,
                "source_label": SOURCE_LABEL,
                "verification_state": "external_candidate",
                "draft_eligible": False,
                "official_origin": True,
                "is_degraded": False,
                "is_fast_digest": False,
            }
        )
    if not items:
        return {
            "ok": False,
            "success": False,
            "error": "no_legaltech_judgment_matches",
            "source": SOURCE,
            "intent_analysis": analysis if analysis.get("success") else {},
        }
    return {
        "ok": True,
        "success": True,
        "source": SOURCE,
        "source_label": SOURCE_LABEL,
        "query": query,
        "search_query": search_query,
        "items": items,
        "intent_analysis": analysis if analysis.get("success") else {},
        "human_approval_required": True,
    }


def search_laws_via_legaltech(safe_query: str, *, article_number: str = "", limit: int = 3) -> Dict[str, Any]:
    query = str(safe_query or "").strip()
    if len(query) < 2:
        return {"ok": False, "success": False, "error": "missing_safe_query", "source": SOURCE}
    analyze_legal_intent_via_legaltech(query)
    arguments: Dict[str, Any] = {"query": query[:120], "limit": max(1, min(5, int(limit)))}
    if article_number:
        arguments["article_number"] = str(article_number)[:20]
    return call_legaltech_tool("search_taiwan_laws", **arguments)
