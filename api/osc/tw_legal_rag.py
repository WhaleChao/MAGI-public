"""Taiwan Legal RAG (TLR) adapter for MAGI.

This module talks to the public retrieval endpoint used by
``aa0101181514/tw-legal-rag``.  The upstream CLI is MIT licensed and exposes a
small HTTP API: search first, then fetch full-text excerpts with the returned
result token.  MAGI keeps this as a lightweight optional adapter so legal
research can be supplemented by semantic full-judgment retrieval without
vendoring a separate checkout.

Privacy note: only the search query is sent to the public endpoint.  Callers
should pass legal issues / keywords, not confidential case facts.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

DEFAULT_TLR_BASE_URL = "https://tlr.dr-lawbot.com"
SEARCH_PATH = "/v1/search"
FULLTEXT_PATH = "/v1/fulltext"
HEALTH_PATH = "/v1/health"
SOURCE_NAME = "tw_legal_rag_tlr"
SOURCE_LABEL = "Taiwan Legal RAG/TLR 全判決語義檢索"


class TLRRetrievalError(RuntimeError):
    """Raised when TLR cannot serve a request."""


@dataclass
class TLRJudgment:
    rank: int
    doc_id: str
    citation_text: str
    court_name: str
    jdate: str
    snippet: str
    citation_url: str
    citation_markdown: str
    result_token: str
    case_category: Optional[str] = None
    fulltext: Optional[str] = None
    cited_articles: list[str] = field(default_factory=list)


def tw_legal_rag_enabled() -> bool:
    value = str(os.environ.get("MAGI_TWLEGALRAG_ENABLE", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def tw_legal_rag_base_url() -> str:
    return str(
        os.environ.get("MAGI_TWLEGALRAG_BASE_URL")
        or os.environ.get("TWLEGALRAG_TLR_BASE_URL")
        or DEFAULT_TLR_BASE_URL
    ).rstrip("/")


def tw_legal_rag_api_key() -> str:
    return str(os.environ.get("MAGI_TWLEGALRAG_API_KEY") or os.environ.get("TWLEGALRAG_TLR_API_KEY") or "").strip()


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        raise TLRRetrievalError(f"TLR returned unparseable JSON: {exc}") from exc


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_TW_ID_RE = re.compile(r"\b[A-Z][12]\d{8}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"\b(?:09\d{8}|0\d{1,2}[- ]?\d{6,8})\b")
_OSC_CASE_RE = re.compile(r"\b20\d{2}-\d{4}\b")
_LONG_NUMBER_RE = re.compile(r"\b\d{7,}\b")


def sanitize_tlr_query(query: str, *, max_chars: int = 160) -> str:
    """Remove obvious private identifiers before sending a query to TLR."""
    text = str(query or "").strip()
    text = re.sub(r"^(?:@MAGI\s*)", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:實務見解|法律見解|法院見解|查判決|找判決|判決搜尋|搜尋判決|查裁判|找裁判)\s*", "", text).strip()
    text = _EMAIL_RE.sub("電子郵件", text)
    text = _TW_ID_RE.sub("身分證字號", text)
    text = _PHONE_RE.sub("電話", text)
    text = _OSC_CASE_RE.sub("案件編號", text)
    text = _LONG_NUMBER_RE.sub("長數字", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:，,。；;")
    return text[:max_chars]


class TLRClient:
    """Thin HTTP client for TLR retrieval endpoints."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = (base_url or tw_legal_rag_base_url()).rstrip("/")
        self.api_key = api_key or tw_legal_rag_api_key()
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._client = requests.Session()

    def __enter__(self) -> "TLRClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "MAGI legal research adapter (+https://github.com/aa0101181514/tw-legal-rag)",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(url, headers=self._headers(), json=body, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
                continue
            data = _loads_lenient(resp.text)
            if resp.status_code == 429:
                if attempt < self.max_retries:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise TLRRetrievalError("TLR rate limit hit (HTTP 429)")
            if resp.status_code == 503:
                raise TLRRetrievalError("TLR service unavailable (HTTP 503)")
            if resp.status_code >= 400:
                detail = data.get("detail", data) if isinstance(data, dict) else data
                raise TLRRetrievalError(f"TLR error (HTTP {resp.status_code}): {detail}")
            if not isinstance(data, dict):
                raise TLRRetrievalError("TLR returned non-object response")
            return data
        raise TLRRetrievalError(f"TLR request failed after retries: {last_exc}")

    def health(self) -> dict[str, Any]:
        resp = self._client.get(self.base_url + HEALTH_PATH, headers=self._headers(), timeout=self.timeout, allow_redirects=True)
        data = _loads_lenient(resp.text)
        if resp.status_code >= 400:
            raise TLRRetrievalError(f"TLR health error (HTTP {resp.status_code}): {data}")
        return data if isinstance(data, dict) else {"status": "unknown", "raw": data}

    def search(self, query: str, *, search_type: str = "hybrid", max_results: int = 5) -> list[TLRJudgment]:
        if search_type not in {"hybrid", "keyword", "phrase"}:
            raise ValueError("search_type must be hybrid | keyword | phrase")
        max_results = max(1, min(int(max_results), 10))
        data = self._post(SEARCH_PATH, {"query": query, "search_type": search_type, "max_results": max_results})
        out: list[TLRJudgment] = []
        for raw in data.get("results") or []:
            if not isinstance(raw, dict):
                continue
            out.append(
                TLRJudgment(
                    rank=int(raw.get("rank") or len(out) + 1),
                    doc_id=str(raw.get("doc_id") or ""),
                    citation_text=str(raw.get("citation_text") or ""),
                    court_name=str(raw.get("court_name") or ""),
                    jdate=str(raw.get("jdate") or ""),
                    snippet=str(raw.get("snippet") or ""),
                    citation_url=str(raw.get("citation_url") or ""),
                    citation_markdown=str(raw.get("citation_markdown") or ""),
                    result_token=str(raw.get("result_token") or ""),
                    case_category=raw.get("case_category"),
                )
            )
        return out

    def fetch_fulltext(self, judgment: TLRJudgment) -> TLRJudgment:
        if not judgment.result_token:
            raise TLRRetrievalError(f"{judgment.doc_id}: missing result_token")
        data = self._post(FULLTEXT_PATH, {"doc_id": judgment.doc_id, "result_token": judgment.result_token})
        judgment.fulltext = str(data.get("text_excerpt") or "")
        cited = data.get("cited_articles") or []
        judgment.cited_articles = [str(x) for x in cited if str(x).strip()] if isinstance(cited, list) else []
        return judgment

    def search_and_read(
        self,
        query: str,
        *,
        search_type: str = "hybrid",
        max_results: int = 5,
        read_top: Optional[int] = None,
    ) -> list[TLRJudgment]:
        hits = self.search(query, search_type=search_type, max_results=max_results)
        n = len(hits) if read_top is None else min(max(0, int(read_top)), len(hits))
        for hit in hits[:n]:
            try:
                self.fetch_fulltext(hit)
            except TLRRetrievalError:
                pass
        return hits


def tlr_health() -> dict[str, Any]:
    if not tw_legal_rag_enabled():
        return {"ok": False, "enabled": False, "error": "tw_legal_rag_disabled", "source": SOURCE_NAME}
    try:
        with TLRClient(timeout=float(os.environ.get("MAGI_TWLEGALRAG_HEALTH_TIMEOUT_SEC", "6") or "6")) as client:
            health = client.health()
        return {
            "ok": health.get("status") == "ok",
            "enabled": True,
            "base_url": tw_legal_rag_base_url(),
            "status": health.get("status"),
            "retrieval": health.get("retrieval"),
            "source": SOURCE_NAME,
        }
    except Exception as exc:
        return {"ok": False, "enabled": True, "base_url": tw_legal_rag_base_url(), "error": str(exc)[:280], "source": SOURCE_NAME}


def _judgment_to_item(judgment: TLRJudgment, citation_id: str) -> dict[str, Any]:
    fulltext = str(judgment.fulltext or "").strip()
    summary = fulltext or str(judgment.snippet or "").strip()
    summary = re.sub(r"\s+", " ", summary)
    return {
        "title": judgment.citation_text or "台灣裁判",
        "summary_preview": summary[:1200] or "TLR 已命中此裁判；請開啟來源全文核對。",
        "summary_full": summary,
        "url": judgment.citation_url,
        "jid": judgment.doc_id,
        "doc_id": judgment.doc_id,
        "citation_id": citation_id,
        "citation_text": judgment.citation_text,
        "court_name": judgment.court_name,
        "judgment_date": judgment.jdate,
        "case_category": judgment.case_category,
        "cited_articles": judgment.cited_articles,
        "source": SOURCE_NAME,
        "source_label": SOURCE_LABEL,
        "is_degraded": False,
        "is_fast_digest": not bool(fulltext),
        "verification_required": True,
    }


def build_tlr_bundle(query: str, judgments: list[TLRJudgment]) -> dict[str, Any]:
    allowed: list[str] = []
    items: list[dict[str, Any]] = []
    excerpt_limit = max(500, int(os.environ.get("MAGI_TWLEGALRAG_BUNDLE_EXCERPT_CHARS", "6000") or "6000"))
    for idx, judgment in enumerate(judgments, start=1):
        citation_id = f"J{idx}"
        allowed.append(citation_id)
        excerpt = str(judgment.fulltext or "")[:excerpt_limit]
        items.append(
            {
                "citation_id": citation_id,
                "doc_id": judgment.doc_id,
                "citation_text": judgment.citation_text,
                "citation_url": judgment.citation_url,
                "court_name": judgment.court_name,
                "jdate": judgment.jdate,
                "case_category": judgment.case_category,
                "cited_articles": judgment.cited_articles,
                "listing": judgment.snippet,
                "fulltext_excerpt": excerpt,
                "fulltext_truncated": bool(judgment.fulltext and len(judgment.fulltext) > excerpt_limit),
            }
        )
    return {
        "schema": "magi.twlegalrag.bundle/v1",
        "upstream_schema": "twlegalrag.bundle/v1",
        "query": query,
        "source": SOURCE_LABEL,
        "retrieval_only": True,
        "allowed_citations": allowed,
        "judgments": items,
        "verification_instructions": {
            "required": True,
            "rules": [
                "只能引用本 bundle 中的 citation_id。",
                "裁判字號若不在 bundle 中，必須標示為未驗證。",
                "不得把當事人主張當成法院見解。",
                "引用或生成書狀前仍應核對裁判全文。",
            ],
        },
    }


def search_practical_judgments_via_tlr(
    query: str,
    *,
    limit: int = 3,
    fulltext_limit: int = 1,
    search_type: str = "hybrid",
) -> Dict[str, Any]:
    if not tw_legal_rag_enabled():
        return {"success": False, "ok": False, "error": "tw_legal_rag_disabled", "source": SOURCE_NAME}
    safe_query = sanitize_tlr_query(query)
    if not safe_query:
        return {"success": False, "ok": False, "error": "missing_or_private_query", "source": SOURCE_NAME}
    try:
        with TLRClient(timeout=float(os.environ.get("MAGI_TWLEGALRAG_TIMEOUT_SEC", "30") or "30")) as client:
            hits = client.search_and_read(
                safe_query,
                search_type=search_type,
                max_results=max(1, min(10, int(limit))),
                read_top=max(0, int(fulltext_limit)),
            )
    except Exception as exc:
        return {"success": False, "ok": False, "error": str(exc)[:280], "source": SOURCE_NAME, "query": safe_query}
    if not hits:
        return {"success": False, "ok": False, "error": "no_tlr_matches", "source": SOURCE_NAME, "query": safe_query}
    items = [_judgment_to_item(hit, f"J{idx}") for idx, hit in enumerate(hits[: max(1, int(limit))], start=1)]
    return {
        "success": True,
        "ok": True,
        "source": SOURCE_NAME,
        "source_label": SOURCE_LABEL,
        "query": safe_query,
        "items": items,
        "bundle": build_tlr_bundle(safe_query, hits[: max(1, int(limit))]),
    }


_CASE_CITATION_RE = re.compile(
    r"(?:最高法院|最高行政法院|臺灣[\u4e00-\u9fff]{1,6}法院|臺北高等行政法院|高雄高等行政法院)?\s*"
    r"\d{2,3}\s*年?度\s*[\u4e00-\u9fff]{1,10}\s*字\s*第\s*\d+\s*號"
)


def _canonical_citation_key(text: str) -> str:
    s = str(text or "").replace("台", "臺")
    s = re.sub(r"\s+", "", s)
    match = re.search(r"(\d{2,3})年?度([\u4e00-\u9fff]{1,10})字第?(\d+)號", s)
    if not match:
        return ""
    return f"{int(match.group(1))}:{match.group(2)}:{int(match.group(3))}"


def citation_check_against_tlr_bundle(answer_text: str, bundle: dict[str, Any]) -> dict[str, Any]:
    """Deterministic bundle-level citation check for MAGI-generated answers."""
    judgments = [j for j in bundle.get("judgments") or [] if isinstance(j, dict)]
    keys: dict[str, dict[str, Any]] = {}
    for judgment in judgments:
        for source in (judgment.get("citation_text"), judgment.get("doc_id")):
            key = _canonical_citation_key(str(source or ""))
            if key:
                keys.setdefault(key, judgment)
    citations = []
    seen: set[str] = set()
    for match in _CASE_CITATION_RE.finditer(str(answer_text or "")):
        citation = match.group(0).strip()
        key = _canonical_citation_key(citation)
        if not key or key in seen:
            continue
        seen.add(key)
        citations.append({"citation_text": citation, "key": key, "in_bundle": key in keys})
    out_of_bundle = [c for c in citations if not c["in_bundle"]]
    overall = "fail" if out_of_bundle else "pass"
    if not citations:
        overall = "needs_review"
    return {
        "ok": overall != "fail",
        "overall": overall,
        "citations_found": len(citations),
        "in_bundle": len(citations) - len(out_of_bundle),
        "out_of_bundle": len(out_of_bundle),
        "citations": citations,
        "source": SOURCE_NAME,
    }
