from __future__ import annotations

import re
from typing import Dict, List

from skills.engine.chinese_nlp import extract_keywords

_CASE_RE = re.compile(r"\d{2,3}年度[^\s]{1,12}字第?\d+號?")
_ARTICLE_RE = re.compile(r"[^\s]{1,12}法第?\s*\d+(?:-\d+)?\s*條")
_LAW_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,20}(?:法|條例|規則|通則|施行法|施行細則)")
_LEGAL_CONCEPT_RE = re.compile(r"[\u4e00-\u9fff]{2,12}(?:行為|責任|契約|損害|賠償|義務|權利)")
_SHORT_QUERY_RE = re.compile(r"^[\u3400-\u9fffA-Za-z0-9第條年度字號\-（）()、，／/]{2,32}$")


def extract_entities(text: str, max_keywords: int = 8) -> List[Dict[str, str]]:
    raw = str(text or "").strip()
    if not raw:
        return []

    entities: List[Dict[str, str]] = []
    seen = set()

    def _push(kind: str, value: str) -> None:
        normalized = str(value or "").strip()
        key = (kind, normalized)
        if not normalized or key in seen:
            return
        seen.add(key)
        entities.append({"kind": kind, "value": normalized})

    for pattern, kind in ((_CASE_RE, "case_number"), (_ARTICLE_RE, "article"), (_LAW_NAME_RE, "law_name")):
        for match in pattern.finditer(raw):
            _push(kind, match.group(0))

    for match in _LEGAL_CONCEPT_RE.finditer(raw):
        _push("legal_concept", match.group(0))

    # Short legal queries are the common Graph-RAG hot path. Reuse the raw query
    # instead of paying the full Chinese segmentation cost on every recall.
    if _SHORT_QUERY_RE.fullmatch(raw):
        _push("keyword", raw)
        if entities:
            return entities

    for keyword in extract_keywords(raw, max_keywords=max_keywords):
        _push("keyword", keyword)

    return entities
