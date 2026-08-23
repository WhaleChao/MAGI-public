"""Strict provenance helpers for official Taiwanese judgment candidates.

The helpers in this module are deliberately independent from storage and
presentation code.  An MCP response is discovery evidence only until its JID,
official URL, full text and date agree with this contract.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


OFFICIAL_JUDGMENT_HOSTS = {"judgment.judicial.gov.tw", "data.judicial.gov.tw"}
OFFICIAL_JID_RE = re.compile(
    r"^[A-Z0-9]{2,12},\d{2,3},[^,\s]{1,24},\d{1,10},\d{8},\d{1,3}$"
)


def normalize_judgment_date(value: Any) -> str:
    """Return a validated Gregorian ISO date from ROC/Gregorian input."""

    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = re.fullmatch(
            r"(?:民國\s*)?(\d{1,3})(?:\s*年|[-/.])(\d{1,2})(?:\s*月|[-/.])(\d{1,2})(?:\s*日)?",
            text,
        )
        if not match:
            return ""
        year, month, day = (int(part) for part in match.groups())
        year += 1911
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def is_official_judgment_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and str(parsed.hostname or "").lower() in OFFICIAL_JUDGMENT_HOSTS


def _url_jid_matches(url: str, jid: str) -> bool:
    parsed = urlparse(url)
    if str(parsed.hostname or "").lower() == "judgment.judicial.gov.tw":
        values = parse_qs(parsed.query).get("id") or []
        return len(values) == 1 and unquote(str(values[0])) == jid
    # data.judicial.gov.tw is an authenticated official API.  Its response is
    # bound by the exact JID field rather than an HTML query parameter.
    return str(parsed.hostname or "").lower() == "data.judicial.gov.tw"


def official_judgment_page_url(jid: Any, source_url: Any = "") -> str:
    normalized = str(jid or "").strip()
    if OFFICIAL_JID_RE.fullmatch(normalized):
        from urllib.parse import quote

        return (
            "https://judgment.judicial.gov.tw/FJUD/data.aspx"
            f"?ty=JD&id={quote(normalized, safe='')}&ot=in"
        )
    fallback = str(source_url or "").strip()
    if not is_official_judgment_url(fallback):
        return ""
    return fallback if str(urlparse(fallback).hostname or "").lower() == "judgment.judicial.gov.tw" else ""


def _date_from_full_text(full_text: str) -> str:
    head = str(full_text or "")[:2200]
    for pattern in (
        r"中\s*華\s*民\s*國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"民\s*國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    ):
        match = re.search(pattern, head)
        if match:
            return normalize_judgment_date("-".join(match.groups()))
    return ""


def validate_official_judgment_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate a full-text MCP candidate without trusting its summary."""

    reasons: list[str] = []
    jid = str(candidate.get("jid") or candidate.get("doc_id") or "").strip()
    source_url = str(candidate.get("source_url") or candidate.get("url") or "").strip()
    full_text = str(candidate.get("full_text") or candidate.get("content") or "").strip()
    if not OFFICIAL_JID_RE.fullmatch(jid):
        reasons.append("missing_or_invalid_official_jid")
    if candidate.get("official_origin") is not True:
        reasons.append("official_origin_not_verified")
    if not is_official_judgment_url(source_url):
        reasons.append("unofficial_source_url")
    elif jid and not _url_jid_matches(source_url, jid):
        reasons.append("official_url_jid_mismatch")
    # A complete simplified judgment can legitimately be short.  Structural
    # consumers perform their own signature/main/holding checks, so this gate
    # only distinguishes real judgment text from a title/snippet.
    if len(full_text) < 40:
        reasons.append("missing_official_fulltext")
    raw_date = candidate.get("judgment_date") or candidate.get("date")
    normalized_date = normalize_judgment_date(raw_date) or _date_from_full_text(full_text)
    if raw_date and not normalized_date:
        reasons.append("judgment_date_invalid")
    return {
        "ok": not reasons,
        "exclusion_codes": reasons,
        "jid": jid,
        "source_url": official_judgment_page_url(jid, source_url),
        "judgment_date": normalized_date,
        "full_text": full_text if not reasons else "",
        "full_text_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest() if full_text else "",
        "pii_included": False,
    }
