"""Optional adapter for lawchat-oss/mcp-taiwan-legal-db.

The upstream project is kept outside git under `.runtime/mcp-taiwan-legal-db`.
This module imports it only when present, so public MAGI builds do not depend on
that checkout and private deployments can enable it by installing the MCP repo.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ROOT = _MAGI_ROOT / ".runtime" / "mcp-taiwan-legal-db"
_DEFAULT_CACHE = _MAGI_ROOT / ".runtime" / "taiwan_legal_mcp" / "cache.sqlite3"
_TOOLS = {
    "search_judgments",
    "get_judgment",
    "query_regulation",
    "get_pcode",
    "search_regulations",
    "get_interpretation",
    "search_interpretations",
    "get_citations",
}


def taiwan_legal_mcp_root() -> Path:
    return Path(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_ROOT") or _DEFAULT_ROOT).expanduser()


def taiwan_legal_mcp_available() -> bool:
    root = taiwan_legal_mcp_root()
    return (root / "mcp_server" / "server.py").exists()


def taiwan_legal_mcp_enabled() -> bool:
    value = str(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_ENABLE", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _install_import_path(root: Path) -> None:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@asynccontextmanager
async def _server_context(root: Optional[Path] = None):
    root = root or taiwan_legal_mcp_root()
    if not (root / "mcp_server" / "server.py").exists():
        raise RuntimeError(f"taiwan_legal_mcp_not_installed:{root}")
    _install_import_path(root)

    from mcp_server import server  # type: ignore
    from mcp_server.cache.db import CacheDB  # type: ignore
    from mcp_server.tools.judicial_doc import JudgmentDocClient  # type: ignore
    from mcp_server.tools.judicial_search import JudicialSearchClient  # type: ignore
    from mcp_server.tools.regulations import RegulationClient  # type: ignore
    from mcp_server.tools.waf_bypass import JudicialWAFBypass  # type: ignore

    cache_path = Path(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_CACHE") or _DEFAULT_CACHE).expanduser()
    cache = CacheDB(cache_path)
    await cache.initialize()
    waf = JudicialWAFBypass()
    reg_client = RegulationClient(cache)
    jud_search = JudicialSearchClient(cache, waf)
    jud_doc = JudgmentDocClient(cache, waf)

    previous = {
        "cache": getattr(server, "cache", None),
        "waf": getattr(server, "waf", None),
        "reg_client": getattr(server, "reg_client", None),
        "jud_search": getattr(server, "jud_search", None),
        "jud_doc": getattr(server, "jud_doc", None),
    }
    server.cache = cache
    server.waf = waf
    server.reg_client = reg_client
    server.jud_search = jud_search
    server.jud_doc = jud_doc
    try:
        yield server
    finally:
        for client in (reg_client, jud_search, jud_doc):
            try:
                await client.close()
            except Exception:
                pass
        try:
            await cache.close()
        except Exception:
            pass
        for key, value in previous.items():
            setattr(server, key, value)


async def call_taiwan_legal_tool_async(tool_name: str, **kwargs: Any) -> Dict[str, Any]:
    if not taiwan_legal_mcp_enabled():
        return {"ok": False, "success": False, "error": "taiwan_legal_mcp_disabled"}
    if tool_name not in _TOOLS:
        return {"ok": False, "success": False, "error": f"unsupported_tool:{tool_name}"}
    try:
        async with _server_context() as server:
            fn = getattr(server, tool_name)
            result = await _maybe_await(fn(**kwargs))
            if isinstance(result, dict):
                result.setdefault("ok", bool(result.get("success", True)))
                result.setdefault("tool", tool_name)
                result.setdefault("source", "taiwan_legal_mcp")
                return result
            return {"ok": True, "success": True, "tool": tool_name, "source": "taiwan_legal_mcp", "result": result}
    except Exception as exc:
        return {
            "ok": False,
            "success": False,
            "tool": tool_name,
            "source": "taiwan_legal_mcp",
            "error": str(exc)[:300],
        }


def call_taiwan_legal_tool(tool_name: str, **kwargs: Any) -> Dict[str, Any]:
    return asyncio.run(call_taiwan_legal_tool_async(tool_name, **kwargs))


_CASE_NO_RE = re.compile(r"(?P<year>\d{2,3})\s*年?度?\s*(?P<word>[\u4e00-\u9fff]{1,8}?)\s*字?\s*第?\s*(?P<number>\d+)\s*號?")


def parse_taiwan_case_number(text: str) -> Dict[str, Any]:
    match = _CASE_NO_RE.search(str(text or ""))
    if not match:
        return {}
    return {
        "year_from": int(match.group("year")),
        "year_to": int(match.group("year")),
        "case_word": match.group("word"),
        "case_number": match.group("number"),
    }


def _first_text(*values: Any, limit: int = 900) -> str:
    parts: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            parts.append(re.sub(r"\s+", " ", text))
    return " ".join(parts)[:limit]


def _result_items(search_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = search_result.get("results")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    items = search_result.get("items")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    return []


def _dedupe_key(item: Dict[str, Any]) -> str:
    return str(item.get("jid") or item.get("url") or item.get("case_id") or item.get("title") or "").strip()


async def search_practical_judgments_via_mcp_async(
    query: str,
    *,
    case_type: str = "",
    limit: int = 3,
    fulltext_limit: int = 1,
) -> Dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        return {"success": False, "error": "missing_query", "source": "taiwan_legal_mcp"}

    params: Dict[str, Any] = {"case_type": case_type, "max_results": max(1, min(10, int(limit)))}
    case_params = parse_taiwan_case_number(query)
    if case_params:
        params.update(case_params)
    else:
        params["keyword"] = query

    search = await call_taiwan_legal_tool_async("search_judgments", **params)
    if not search.get("success"):
        return search

    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _result_items(search):
        jid = str(raw.get("jid") or "").strip()
        detail: Dict[str, Any] = {}
        if jid and len(normalized) < max(0, int(fulltext_limit)):
            detail = await call_taiwan_legal_tool_async("get_judgment", jid=jid)
            if not detail.get("success"):
                detail = {}

        court = str(detail.get("court") or raw.get("court") or "").strip()
        case_id = str(detail.get("case_id") or raw.get("case_id") or "").strip()
        title = " ".join(part for part in [court, case_id] if part).strip() or case_id or jid or "司法院裁判"
        summary = _first_text(
            detail.get("main_text"),
            detail.get("reasoning"),
            raw.get("main_text"),
            raw.get("summary"),
            raw.get("snippet"),
            limit=1200,
        )
        item = {
            "title": title,
            "summary_preview": summary or "已由司法院公開資料命中，請開啟來源全文核對。",
            "summary_full": summary,
            "url": detail.get("source_url") or raw.get("url") or "",
            "jid": jid,
            "source": "taiwan_legal_mcp",
            "source_label": "台灣法律資料庫 MCP（司法院公開資料）",
            "is_degraded": False,
            "is_fast_digest": False,
        }
        key = _dedupe_key(item)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        normalized.append(item)

    if not normalized:
        return {"success": False, "error": "no_mcp_judgment_matches", "source": "taiwan_legal_mcp"}
    return {
        "success": True,
        "source": "taiwan_legal_mcp",
        "source_label": "台灣法律資料庫 MCP（司法院公開資料）",
        "query": query,
        "items": normalized[: max(1, int(limit))],
    }


def search_practical_judgments_via_mcp(query: str, *, case_type: str = "", limit: int = 3, fulltext_limit: int = 1) -> Dict[str, Any]:
    return asyncio.run(
        search_practical_judgments_via_mcp_async(
            query,
            case_type=case_type,
            limit=limit,
            fulltext_limit=fulltext_limit,
        )
    )


def merge_judgment_sources(primary: Dict[str, Any], supplemental: Dict[str, Any], *, limit: int = 3) -> Dict[str, Any]:
    if not supplemental.get("success"):
        return primary
    primary_items = primary.get("items") if isinstance(primary.get("items"), list) else []
    if not primary.get("success") or not primary_items:
        return supplemental

    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(primary_items) + list(supplemental.get("items") or []):
        if not isinstance(item, dict):
            continue
        key = _dedupe_key(item)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(item)
        if len(merged) >= max(1, int(limit)):
            break
    source_label = str(primary.get("source_label") or "本地實務見解庫")
    sup_label = str(supplemental.get("source_label") or "").strip()
    if sup_label and sup_label not in source_label:
        source_label = f"{source_label} + {sup_label}"
    return {**primary, "success": True, "source_label": source_label, "items": merged}
