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

_NAV_SKIP_PATTERNS = re.compile(
    r"(首頁|回首頁|home|about|contact|sitemap|privacy|login|logout|search|register"
    r"|facebook|twitter|youtube|instagram|linkedin|rss|訂閱|分享|列印|print"
    r"|next|prev|previous|older|newer|下一|上一|更多|more|載入|loading"
    r"|skip to|jump to|回到頂|top of page|^#)",
    re.IGNORECASE,
)

_MIN_TITLE_LEN = 8
_MIN_PATH_DEPTH = 1  # skip bare-root hrefs like "/"


def _autodiscover_rss(html: str, base_url: str) -> Optional[str]:
    """Return RSS/Atom feed URL if autodiscovery link found in <head>."""
    from urllib.parse import urljoin
    for m in re.finditer(
        r'<link[^>]+type=["\']application/(rss|atom)\+xml["\'][^>]*>',
        html, re.IGNORECASE
    ):
        href_m = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        if href_m:
            return urljoin(base_url, href_m.group(1))
    return None


def _extract_article_links(html: str, base_url: str,
                            max_links: int) -> List[Dict[str, Any]]:
    """
    Extract candidate article links from an HTML listing page.
    Returns list of {title, url, snippet} dicts.
    """
    from urllib.parse import urljoin, urlparse

    base_parsed = urlparse(base_url)
    base_netloc = base_parsed.netloc.lower()

    # Strip <script> / <style> / <nav> / <footer> / <header> blocks first
    clean = re.sub(
        r"<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>",
        " ", html, flags=re.IGNORECASE | re.DOTALL,
    )

    # Extract all <a> tags
    anchors = re.findall(r'<a\s[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
                         clean, re.IGNORECASE | re.DOTALL)

    seen_urls: set = set()
    results: List[Dict[str, Any]] = []

    for raw_href, raw_text in anchors:
        if len(results) >= max_links:
            break

        title = _strip_html(raw_text).strip()
        if len(title) < _MIN_TITLE_LEN:
            continue
        if _NAV_SKIP_PATTERNS.search(title):
            continue

        href = raw_href.strip()
        # Skip mailto / javascript / fragment-only
        if re.match(r"(mailto:|javascript:|tel:)", href, re.IGNORECASE):
            continue

        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        # Only same-domain or sub-domain links
        link_netloc = parsed.netloc.lower()
        if link_netloc and link_netloc != base_netloc:
            # Allow sub-domains of base domain
            base_root = ".".join(base_netloc.split(".")[-2:])
            if not link_netloc.endswith(base_root):
                continue

        # Require some path depth to skip bare root hrefs
        path = parsed.path.rstrip("/")
        if path.count("/") < _MIN_PATH_DEPTH:
            continue

        # Skip obvious non-article extensions
        if re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|ico|pdf|zip|xml)$",
                     parsed.path, re.IGNORECASE):
            continue

        # Dedup
        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)

        results.append({
            "title": title[:200],
            "url": abs_url,
            "snippet": "",
            "raw": "",
        })

    return results


def fetch_html(url: str, *, lang_hint: Optional[str] = None,
               max_links: int = 20) -> List[Dict[str, Any]]:
    """
    Improved HTML listing-page scrape:
    1. Autodiscover RSS/Atom feed — if found, delegate to fetch_rss().
    2. Otherwise extract individual article links from <a> tags.
    3. Falls back to returning the page itself if no links found.
    """
    content = _http_get(url)
    if not content:
        return []

    # Step 1: RSS autodiscovery
    rss_url = _autodiscover_rss(content, url)
    if rss_url:
        logger.info("fetch_html: found RSS feed %s for %s", rss_url, url)
        items = fetch_rss(rss_url, lang_hint=lang_hint)
        if items:
            return items

    # Step 2: Extract article links
    items = _extract_article_links(content, url, max_links=max_links)

    if items:
        now_iso = datetime.now(timezone.utc).isoformat()
        result = []
        for item in items[:_MAX_ENTRIES_PER_SOURCE]:
            result.append({
                "title": item["title"],
                "url": item["url"],
                "published": now_iso,
                "snippet": item["snippet"],
                "raw": item["raw"],
                "lang_hint": lang_hint,
            })
        return result

    # Step 3: Fallback — return page itself (last resort)
    m = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    page_title = _strip_html(m.group(1)) if m else url
    body = _strip_html(content)
    return [{
        "title": page_title,
        "url": url,
        "published": datetime.now(timezone.utc).isoformat(),
        "snippet": body[:600],
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
