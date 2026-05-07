# -*- coding: utf-8 -*-
"""
MAGI Scrapling adapter.

Feature-flagged HTML fetching layer that keeps the caller-facing API stable.
When Scrapling is disabled or unavailable, callers should fall back to the
existing requests + BeautifulSoup implementation.
"""

from __future__ import annotations

import logging
import os
import json
import re
from typing import Any, Dict

from bs4 import BeautifulSoup

logger = logging.getLogger("scraping_adapter")
_SCRAPLING_LOGGER = logging.getLogger("scrapling")


def scrapling_enabled() -> bool:
    return os.environ.get("MAGI_USE_SCRAPLING", "0").strip().lower() in {"1", "true", "yes", "on"}


def _extract_text_from_html(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
        element.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    main_content = soup.find("main") or soup.find("article") or soup.find("body") or soup
    text = main_content.get_text(separator="\n", strip=True)
    return {
        "title": title,
        "content": text,
        "html": str(soup),
    }


def _extract_json_payload(raw: Any) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    stripped = text.strip()
    if stripped.startswith("<"):
        match = re.search(r"<body[^>]*>(.*)</body>", stripped, re.IGNORECASE | re.DOTALL)
        if match:
            stripped = match.group(1).strip()
    return stripped


def fetch_page(url: str, timeout: int = 20) -> Dict[str, Any]:
    """
    Attempt to fetch a page with Scrapling.

    Returns:
      {"use_fallback": True} when callers should continue with the legacy path.
      Otherwise:
      {
        "use_fallback": False,
        "success": bool,
        "url": str,
        "title": str,
        "content": str,
        "html": str,
        "status_code": int,
        "engine": "scrapling",
        "error": str|None,
      }
    """
    if not scrapling_enabled():
        return {"use_fallback": True}

    try:
        from scrapling import Fetcher
    except Exception as exc:
        logger.warning("Scrapling import failed: %s", exc)
        return {"use_fallback": True, "error": f"scrapling_import_failed: {exc}"}

    try:
        try:
            Fetcher.configure(auto_match=True)
        except Exception:
            logger.debug("Scrapling Fetcher.configure(auto_match=True) unavailable", exc_info=True)
        previous_level = _SCRAPLING_LOGGER.level
        try:
            _SCRAPLING_LOGGER.setLevel(logging.ERROR)
            fetcher = Fetcher()
        finally:
            _SCRAPLING_LOGGER.setLevel(previous_level)
        response = fetcher.get(url, timeout=max(5, int(timeout)))
        status_code = int(getattr(response, "status", 0) or 0)
        html = str(getattr(response, "html_content", "") or "")
        if status_code >= 400 or not html.strip():
            return {
                "use_fallback": False,
                "success": False,
                "url": url,
                "title": "",
                "content": "",
                "html": html,
                "status_code": status_code,
                "engine": "scrapling",
                "error": f"http_{status_code}" if status_code else "empty_html",
            }

        extracted = _extract_text_from_html(html)
        return {
            "use_fallback": False,
            "success": True,
            "url": str(getattr(response, "url", "") or url),
            "title": extracted["title"],
            "content": extracted["content"],
            "html": html,
            "status_code": status_code,
            "engine": "scrapling",
            "error": None,
        }
    except Exception as exc:
        logger.warning("Scrapling fetch failed for %s: %s", url, exc)
        return {"use_fallback": True, "error": f"scrapling_fetch_failed: {exc}"}


def fetch_json(url: str, *, headers: Dict[str, str] | None = None, timeout: int = 20) -> Dict[str, Any]:
    """
    Attempt to fetch a JSON endpoint with Scrapling while keeping a clean fallback
    contract for callers that still want to use requests.
    """
    if not scrapling_enabled():
        return {"use_fallback": True}

    try:
        from scrapling import Fetcher
    except Exception as exc:
        logger.warning("Scrapling import failed for JSON fetch: %s", exc)
        return {"use_fallback": True, "error": f"scrapling_import_failed: {exc}"}

    try:
        previous_level = _SCRAPLING_LOGGER.level
        try:
            _SCRAPLING_LOGGER.setLevel(logging.ERROR)
            fetcher = Fetcher()
        finally:
            _SCRAPLING_LOGGER.setLevel(previous_level)
        response = fetcher.get(url, headers=headers or {}, timeout=max(5, int(timeout)))
        status_code = int(getattr(response, "status", 0) or 0)
        body = (
            getattr(response, "text", None)
            or getattr(response, "body", None)
            or getattr(response, "html_content", None)
            or ""
        )
        if status_code >= 400:
            return {
                "use_fallback": False,
                "success": False,
                "status_code": status_code,
                "engine": "scrapling",
                "error": f"http_{status_code}",
            }
        data = json.loads(_extract_json_payload(body))
        return {
            "use_fallback": False,
            "success": True,
            "status_code": status_code,
            "engine": "scrapling",
            "data": data,
            "error": None,
        }
    except Exception as exc:
        logger.warning("Scrapling JSON fetch failed for %s: %s", url, exc)
        return {"use_fallback": True, "error": f"scrapling_json_fetch_failed: {exc}"}
