#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_news.py - lightweight market news fetcher.

Uses public RSS search feeds and stores a short-lived cache so the committee
receives attributed headlines instead of synthetic placeholder news.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import parse, request

_SKILL_DIR = Path(__file__).resolve().parent
_MAGI_ROOT = _SKILL_DIR.parents[1]
_AGENT_DIR = _MAGI_ROOT / ".agent"
_NEWS_CACHE_PATH = _AGENT_DIR / "market_news_cache.json"

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or str(default))
    except Exception:
        return default


def _strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(text or ""), flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _load_cache() -> Dict[str, Any]:
    try:
        if _NEWS_CACHE_PATH.exists():
            data = json.loads(_NEWS_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("market news cache load failed", exc_info=True)
    return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    try:
        _AGENT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _NEWS_CACHE_PATH.with_suffix(_NEWS_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_NEWS_CACHE_PATH)
    except Exception:
        logger.debug("market news cache save failed", exc_info=True)


def _fetch_text(url: str, timeout: int) -> str:
    req = request.Request(
        url,
        headers={
            "User-Agent": "MAGI-market-news/1.0",
            "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _node_text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    return (found.text if found is not None and found.text else "").strip()


def _split_title_source(title: str, default_source: str) -> Tuple[str, str]:
    text = str(title or "").strip()
    if " - " not in text:
        return text, default_source
    head, tail = text.rsplit(" - ", 1)
    head = head.strip()
    tail = tail.strip()
    if head and tail and len(tail) <= 80:
        return head, tail
    return text, default_source


def _parse_news_rss(raw: str, source_name: str, max_items: int) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return items

    for item in root.iter("item"):
        title = _strip_html(_node_text(item, "title"))
        title, source = _split_title_source(title, source_name)
        url = _node_text(item, "link")
        published = _node_text(item, "pubDate")
        snippet = _strip_html(_node_text(item, "description"))[:260]
        if title and url:
            items.append({
                "title": title,
                "url": url,
                "source": source,
                "published": published,
                "snippet": snippet,
            })
        if len(items) >= max_items:
            return items

    if items:
        return items

    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(atom_ns + "entry"):
        title = _strip_html(_node_text(entry, atom_ns + "title"))
        title, source = _split_title_source(title, source_name)
        link_el = entry.find(atom_ns + "link")
        url = (link_el.get("href", "") if link_el is not None else "").strip()
        published = _node_text(entry, atom_ns + "updated") or _node_text(entry, atom_ns + "published")
        summary = _strip_html(_node_text(entry, atom_ns + "summary"))[:260]
        if title and url:
            items.append({
                "title": title,
                "url": url,
                "source": source,
                "published": published,
                "snippet": summary,
            })
        if len(items) >= max_items:
            break
    return items


def _dedupe(items: List[Dict[str, str]], max_items: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        key = re.sub(r"\W+", "", title.lower())[:120] or url
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


def _build_queries(symbol: str, label: str, market: str) -> List[str]:
    sym = str(symbol or "").strip()
    base_sym = sym.split(".")[0]
    lab = str(label or "").strip()
    if market == "TW":
        main = lab if lab and lab != base_sym else base_sym
        return [
            f"{main} {base_sym} 股票 財報 OR 營收 when:30d",
            f"{main} {base_sym} 股價 新聞 when:30d",
        ]
    name = lab if lab and lab.upper() != base_sym.upper() else base_sym.upper()
    return [
        f"{name} {base_sym.upper()} stock earnings OR revenue when:30d",
        f"{name} {base_sym.upper()} stock news when:30d",
    ]


def fetch_market_news(symbol: str, label: str = "", market: str = "US", max_items: int = 5) -> List[Dict[str, str]]:
    """Return attributed recent headlines for one ticker.

    Empty list means no verified headlines were fetched; callers should treat
    sentiment as unavailable instead of asking the LLM to improvise.
    """
    if str(os.environ.get("MAGI_MARKET_NEWS_ENABLE", "1")).strip().lower() in {"0", "false", "off", "no"}:
        return []

    max_items = max(1, min(int(max_items or 5), 10))
    timeout = max(2, _env_int("MAGI_MARKET_NEWS_TIMEOUT_SEC", 8))
    ttl = max(60, _env_int("MAGI_MARKET_NEWS_CACHE_TTL_SEC", 21600))
    sym = str(symbol or "").strip().upper()
    key = f"{sym}|{str(label or '').strip()}|{str(market or '').strip().upper()}|{max_items}"
    now = int(time.time())

    cache = _load_cache()
    cached = cache.get(key) if isinstance(cache.get(key), dict) else None
    if cached and now - int(cached.get("ts") or 0) < ttl:
        rows = cached.get("items")
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)][:max_items]

    all_items: List[Dict[str, str]] = []
    for query in _build_queries(sym, label, str(market or "US").upper()):
        rss_url = (
            "https://news.google.com/rss/search?"
            + parse.urlencode({
                "q": query,
                "hl": "zh-TW",
                "gl": "TW",
                "ceid": "TW:zh-Hant",
            })
        )
        try:
            raw = _fetch_text(rss_url, timeout=timeout)
            all_items.extend(_parse_news_rss(raw, "Google News", max_items=max_items))
        except Exception as e:
            logger.debug("market news fetch failed for %s: %s", sym, e)
        if len(all_items) >= max_items:
            break

    items = _dedupe(all_items, max_items)
    cache[key] = {"ts": now, "items": items}
    _save_cache(cache)
    return items


def format_news_for_prompt(items: List[Dict[str, str]], max_items: int = 5) -> List[str]:
    """Render compact, source-attributed lines for committee prompts."""
    lines: List[str] = []
    for idx, item in enumerate(items[:max_items], 1):
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "unknown").strip()
        published = str(item.get("published") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title:
            continue
        meta = source
        if published:
            meta += f", {published}"
        if url:
            meta += f", {url}"
        lines.append(f"{idx}. {title} ({meta})")
    return lines
