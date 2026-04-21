# -*- coding: utf-8 -*-
"""
Digest formatter for research-brief.

Produces the user-specified format:

    **<繁中標題> / <original title>**
    <繁中摘要 120 字內>
    🏷 <tag1> <tag2> <tag3>
    🔗 <原文連結> · <lang> · <source>

Uses local LLM for summary (E4B) and extracts tags via keyword hits.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from urllib.parse import urlparse

logger = logging.getLogger("research-brief.digest")

_MAX_SUMMARY_CHARS = 120
_MAX_TITLE_CHARS = 80


def _hostname(u: str) -> str:
    try:
        h = urlparse(u).hostname or u
        return h.replace("www.", "")
    except Exception:
        return u[:40]


def _extract_tags(text: str, keyword_pool: List[str], limit: int = 3) -> List[str]:
    """Pick up to `limit` tags from keyword_pool that appear in the text."""
    if not text:
        return []
    hits: List[str] = []
    low = text.lower()
    for kw in keyword_pool:
        k = kw.strip()
        if not k:
            continue
        if k.lower() in low or k in text:
            if k not in hits:
                hits.append(k)
        if len(hits) >= limit:
            break
    return hits


def _truncate_zh(text: str, n: int) -> str:
    """Safely truncate at character boundary."""
    if not text:
        return ""
    t = text.replace("\n", " ").strip()
    if len(t) <= n:
        return t
    return t[:n].rstrip() + "…"


def _zh_summarize_local(text: str, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    """
    Generate a concise zh-Hant summary using local E4B (grounded_ai._generate_local).
    Fallback: first N chars with ellipsis.
    """
    text = (text or "").strip()
    if not text:
        return ""
    # If already short enough, trim and return
    if len(text) <= max_chars:
        return text
    try:
        from skills.bridge.grounded_ai import _generate_local  # type: ignore
        prompt = (
            f"你是專業的法律/學術文獻編輯。請用繁體中文將以下內容濃縮為一段 {max_chars} 字以內的摘要，"
            f"保留事實與數字，不加任何前言、引號或 Markdown 標記，只輸出摘要本身：\n\n{text}"
        )
        out = _generate_local(prompt, max_tokens=200, temperature=0.2)
        if out and isinstance(out, str):
            cleaned = out.strip().strip("「」\"'")
            cleaned = re.sub(r"^(摘要[:：]\s*|以下是摘要[:：]?\s*)", "", cleaned)
            return _truncate_zh(cleaned, max_chars)
    except Exception as e:
        logger.debug("local summarizer failed: %s", e)
    return _truncate_zh(text, max_chars)


def _translate_title(title: str, source_lang: Optional[str]) -> str:
    """Translate title to zh-Hant if needed."""
    if not title:
        return ""
    try:
        from translator_bridge import translate_to_zh_hant as _tt
    except Exception:
        try:
            from .translator_bridge import translate_to_zh_hant as _tt
        except Exception:
            return title
    r = _tt(title, source_lang=source_lang)
    return str(r.get("text") or title).strip()


def format_entry(entry: Dict[str, Any], *, source_name: str,
                 keyword_pool: List[str]) -> str:
    """
    Format a single entry into the user-specified block.
    `entry` must have: title, url, snippet/raw, lang_hint, published
    """
    orig_title = str(entry.get("title") or "").strip()
    url = str(entry.get("url") or "").strip()
    raw = str(entry.get("raw") or entry.get("snippet") or "").strip()
    lang = entry.get("lang_hint") or ""

    # Translate body first (so we can summarize in zh)
    try:
        from translator_bridge import translate_to_zh_hant as _tt
    except Exception:
        from .translator_bridge import translate_to_zh_hant as _tt
    body_r = _tt(raw, source_lang=lang)
    zh_body = str(body_r.get("text") or "").strip()
    provider = str(body_r.get("provider") or "")
    degraded = bool(body_r.get("degraded"))
    effective_lang = str(body_r.get("source_lang") or lang or "auto")

    # Summarize in zh-Hant
    zh_summary = _zh_summarize_local(zh_body, _MAX_SUMMARY_CHARS)

    # Translate title (if not zh)
    if effective_lang.startswith("zh"):
        zh_title = orig_title
    else:
        zh_title = _translate_title(orig_title, source_lang=effective_lang)
    zh_title = _truncate_zh(zh_title, _MAX_TITLE_CHARS)

    # Tags
    tags = _extract_tags(orig_title + " " + raw, keyword_pool, limit=3)
    tag_line = "🏷 " + " ".join(f"#{t}" for t in tags) if tags else ""

    # Composition
    title_line = f"**{zh_title} / {orig_title}**" if (zh_title and zh_title != orig_title) else f"**{orig_title}**"
    link_parts = [url, effective_lang or "auto", source_name or _hostname(url)]
    link_line = "🔗 " + " · ".join(p for p in link_parts if p)
    degraded_line = "⚠️ 翻譯降級（Apple 失敗→NIM fallback）" if degraded and provider == "nim_fallback" else ""

    parts = [title_line, zh_summary]
    if tag_line:
        parts.append(tag_line)
    parts.append(link_line)
    if degraded_line:
        parts.append(degraded_line)
    return "\n".join(p for p in parts if p)


def format_digest(namespace: str, entries: List[Dict[str, Any]], *,
                  keyword_pool: List[str]) -> str:
    """Bundle formatted entries into a digest message for a namespace."""
    if not entries:
        return f"📭 **{namespace}** — 今日無新文獻"
    header = f"📚 **研究日報 — {namespace}**（{len(entries)} 則）\n"
    blocks: List[str] = []
    for e in entries:
        try:
            src = str(e.get("source_name") or _hostname(str(e.get("url", ""))))
            blocks.append(format_entry(e, source_name=src, keyword_pool=keyword_pool))
        except Exception as exc:
            logger.warning("format_entry failed for %s: %s", e.get("url"), exc)
            continue
    return header + "\n\n---\n\n".join(blocks)
