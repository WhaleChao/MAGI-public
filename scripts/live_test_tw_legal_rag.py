#!/usr/bin/env python3
"""Live test for MAGI Taiwan Legal RAG integration.

This intentionally calls the public TLR endpoint.  It sends only generic legal
keywords, then verifies MAGI can consume the result in both practical-insight
and judgment-classifier paths.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.blueprints.raziel import _tlr_preview_for_config  # noqa: E402
from api.domains import judgment_flow  # noqa: E402
from api.osc.tw_legal_rag import citation_check_against_tlr_bundle, search_practical_judgments_via_tlr, tlr_health  # noqa: E402


def main() -> int:
    os.environ["MAGI_TWLEGALRAG_ENABLE"] = "1"
    os.environ["MAGI_TWLEGALRAG_AUGMENT"] = "1"
    os.environ["MAGI_TAIWAN_LEGAL_MCP_ENABLE"] = "0"
    raw_queries = os.environ.get("MAGI_TWLEGALRAG_LIVE_QUERY", "通譯 最高法院;消費者債務清理 最高法院;預售屋 遲延交屋")
    queries = [q.strip() for q in raw_queries.split(";") if q.strip()]

    health = tlr_health()
    attempts = []
    query = queries[0] if queries else "通譯 最高法院"
    search = {"success": False, "error": "not_run", "items": []}
    for candidate in queries:
        candidate_search = search_practical_judgments_via_tlr(candidate, limit=2, fulltext_limit=2)
        attempts.append(
            {
                "query": candidate,
                "success": bool(candidate_search.get("success")),
                "count": len(candidate_search.get("items") or []),
                "quality_count": judgment_flow._high_quality_judgment_count(candidate_search),
                "error": candidate_search.get("error"),
            }
        )
        if candidate_search.get("success") and judgment_flow._high_quality_judgment_count(candidate_search) > 0:
            query = candidate
            search = candidate_search
            break
    augmented = judgment_flow._augment_judgments_with_external_sources(
        query,
        {"success": False, "error": "live_primary_intentionally_empty"},
        limit=2,
    )
    raziel_preview = _tlr_preview_for_config({"keyword_query": query}, limit=2)
    bundle = search.get("bundle") if isinstance(search, dict) else {}
    citation_source = ""
    if search.get("success") and search.get("items"):
        first = search["items"][0]
        citation_source = str(first.get("citation_text") or first.get("title") or "")
    citation_check = citation_check_against_tlr_bundle(citation_source, bundle if isinstance(bundle, dict) else {})

    report = {
        "ok": bool(
            health.get("ok")
            and search.get("success")
            and judgment_flow._high_quality_judgment_count(search) > 0
            and augmented.get("success")
            and judgment_flow._high_quality_judgment_count(augmented) > 0
            and raziel_preview.get("ok")
        ),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "attempts": attempts,
        "health": health,
        "search_count": len(search.get("items") or []),
        "search_quality_count": judgment_flow._high_quality_judgment_count(search),
        "search_titles": [item.get("title") for item in (search.get("items") or [])],
        "augmented_source_label": augmented.get("source_label"),
        "augmented_count": len(augmented.get("items") or []),
        "augmented_quality_count": judgment_flow._high_quality_judgment_count(augmented),
        "raziel_preview_count": raziel_preview.get("count"),
        "citation_check": citation_check,
    }
    out_dir = ROOT / ".runtime" / "live_checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"tw_legal_rag_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report_path": str(out_path)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
