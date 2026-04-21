# -*- coding: utf-8 -*-
"""
Fetchers for research-brief: RSS / HTML / JSON adapters.

Returns a list of dict entries:
    {"title": str, "url": str, "published": Optional[str],
     "snippet": str, "raw": str, "lang_hint": Optional[str]}

Scrapling is preferred (per MAGI policy) with requests fallback.
"""
from __future__ import annotations

import html as _html
import json as _json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

logger = logging.getLogger("research-brief.fetchers")

_DEFAULT_TIMEOUT = 15
_MAX_ENTRIES_PER_SOURCE = 30

# Taiwan .gov.tw sites known to have "Missing Subject Key Identifier" TLS cert issue
# under newer OpenSSL; bypass verify for these specific hosts only (same pattern as
# judicial-web-search in CLAUDE.md).
_SSL_BYPASS_HOSTS = {
    "www.humanrights.moj.gov.tw", "humanrights.moj.gov.tw",
    "cons.judicial.gov.tw", "www.cons.judicial.gov.tw",
    "nhrc.cy.gov.tw", "www.nhrc.cy.gov.tw",
    "www.judicial.gov.tw", "judicial.gov.tw",
    "www.cip.gov.tw", "cip.gov.tw",
}


def _strip_html(text: str) -> str:
    if not text:
        return ""
    # Remove script/style blocks first
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _http_get(url: str, timeout: int = _DEFAULT_TIMEOUT) -> Optional[str]:
    """Prefer Scrapling, fallback to requests."""
    try:
        from skills.engine.scraping_adapter import fetch_page
        r = fetch_page(url, timeout=timeout)
        if isinstance(r, dict):
            # scraping_adapter returns {"ok": bool, "text": str, "html": str, ...}
            html = r.get("html") or ""
            text = r.get("text") or ""
            if html:
                return html
            if text:
                return text
    except Exception as e:
        logger.debug("scrapling failed for %s: %s", url, e)
    try:
        import requests
        from urllib.parse import urlparse as _up
        host = (_up(url).hostname or "").lower()
        verify = host not in _SSL_BYPASS_HOSTS
        if not verify:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        resp = requests.get(url, timeout=timeout, verify=verify, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MAGI-research-brief/1.0"
        })
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.warning("requests fallback failed for %s: %s", url, e)
    return None


# ───────── RSS / Atom ─────────

def fetch_rss(url: str, *, lang_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse RSS 2.0 or Atom feed."""
    content = _http_get(url)
    if not content:
        return []
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning("RSS parse error for %s: %s", url, e)
        return []

    # RSS 2.0
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        date_el = item.find("pubDate")
        title = (title_el.text if title_el is not None else "") or ""
        link = (link_el.text if link_el is not None else "") or ""
        snippet = _strip_html((desc_el.text if desc_el is not None else "") or "")
        published = (date_el.text if date_el is not None else "") or ""
        if title and link:
            items.append({
                "title": title.strip(),
                "url": link.strip(),
                "published": published.strip(),
                "snippet": snippet[:400],
                "raw": snippet,
                "lang_hint": lang_hint,
            })
        if len(items) >= _MAX_ENTRIES_PER_SOURCE:
            break

    # Atom
    if not items:
        atom_ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.iter(f"{atom_ns}entry"):
            t = entry.find(f"{atom_ns}title")
            l = entry.find(f"{atom_ns}link")
            s = entry.find(f"{atom_ns}summary")
            if s is None:
                s = entry.find(f"{atom_ns}content")
            d = entry.find(f"{atom_ns}updated")
            if d is None:
                d = entry.find(f"{atom_ns}published")
            title = (t.text if t is not None else "") or ""
            link = l.get("href", "") if l is not None else ""
            snippet = _strip_html((s.text if s is not None else "") or "")
            published = (d.text if d is not None else "") or ""
            if title and link:
                items.append({
                    "title": title.strip(),
                    "url": link.strip(),
                    "published": published.strip(),
                    "snippet": snippet[:400],
                    "raw": snippet,
                    "lang_hint": lang_hint,
                })
            if len(items) >= _MAX_ENTRIES_PER_SOURCE:
                break

    return items


# ───────── JSON API ─────────

def fetch_json_api(url: str, *, lang_hint: Optional[str] = None,
                   item_path: str = "", title_field: str = "title",
                   url_field: str = "url", snippet_field: str = "abstract",
                   date_field: str = "published") -> List[Dict[str, Any]]:
    """Fetch a JSON API and extract entries. item_path like 'data.papers'."""
    content = _http_get(url)
    if not content:
        return []
    try:
        data = _json.loads(content)
    except Exception as e:
        logger.warning("JSON parse error for %s: %s", url, e)
        return []
    nodes: Any = data
    if item_path:
        for seg in item_path.split("."):
            if isinstance(nodes, dict):
                nodes = nodes.get(seg)
            else:
                nodes = None
                break
    if not isinstance(nodes, list):
        return []
    items: List[Dict[str, Any]] = []
    for node in nodes[:_MAX_ENTRIES_PER_SOURCE]:
        if not isinstance(node, dict):
            continue
        title = str(node.get(title_field, "")).strip()
        u = str(node.get(url_field, "")).strip()
        snippet = str(node.get(snippet_field, "") or "")[:400]
        published = str(node.get(date_field, "") or "")
        if title and u:
            items.append({
                "title": title,
                "url": u,
                "published": published,
                "snippet": snippet,
                "raw": snippet,
                "lang_hint": lang_hint,
            })
    return items


# ───────── HTML scrape (generic) ─────────

def fetch_html(url: str, *, lang_hint: Optional[str] = None,
               max_links: int = 20) -> List[Dict[str, Any]]:
    """
    Generic HTML landing-page scrape: extract <a> anchors and snippet the page body.
    Works as a fallback when a site has no RSS.
    """
    content = _http_get(url)
    if not content:
        return []
    # Find title
    m = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    page_title = _strip_html(m.group(1)) if m else url
    body = _strip_html(content)
    snippet = body[:600]
    # Return landing-page as single entry; we intentionally don't over-scrape anchors
    # to avoid drowning the namespace. Dedicated adapters can be added per-source.
    return [{
        "title": page_title,
        "url": url,
        "published": datetime.now(timezone.utc).isoformat(),
        "snippet": snippet,
        "raw": body[:4000],
        "lang_hint": lang_hint,
    }]


# ───────── Dispatcher ─────────

def fetch_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Dispatch by source type. source schema:
        {"url": str, "type": "rss"|"html"|"json", "lang": str, "note": str, "json_opts": dict}
    """
    stype = (source.get("type") or "rss").lower()
    url = source.get("url", "")
    lang = source.get("lang")
    if not url:
        return []
    try:
        if stype == "rss":
            return fetch_rss(url, lang_hint=lang)
        if stype == "json":
            opts = source.get("json_opts") or {}
            return fetch_json_api(url, lang_hint=lang, **opts)
        return fetch_html(url, lang_hint=lang)
    except Exception as e:
        logger.warning("fetch_source failed for %s (%s): %s", url, stype, e)
        return []
