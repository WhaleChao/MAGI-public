#!/usr/bin/env python3
"""Privacy-safe LIVE probe for LegalTech Taiwan Law MCP integration.

The probe uses synthetic legal issues only.  It never sends a party name,
case facts, an internal case number, or a local path, and it never writes to
MAGI's database.  Evidence intentionally records only counts, tool names,
official identifiers/hosts, and SHA-256 digests rather than legal full text.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.domains.judgment_flow import (  # noqa: E402
    _run_direct_taiwan_legal_mcp_lookup,
    build_legal_research_payload,
)
from api.osc.legaltech_taiwan_law_mcp import (  # noqa: E402
    SOURCE,
    analyze_legal_intent_via_legaltech,
    search_laws_via_legaltech,
    search_practical_judgments_via_legaltech,
)


def _digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _official_host(value: object) -> str:
    return str(urllib.parse.urlparse(str(value or "")).hostname or "").lower()


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError(label)


def main() -> int:
    issue = "侵權行為 因果關係 舉證責任"
    intent = analyze_legal_intent_via_legaltech(issue)
    laws = search_laws_via_legaltech("民法 184", article_number="184", limit=2)
    judgments = search_practical_judgments_via_legaltech(issue, case_type="民事", limit=2, fulltext_limit=1)
    direct_answer = _run_direct_taiwan_legal_mcp_lookup("查法條 民法第184條")
    research = build_legal_research_payload(issue, limit=3)

    _require(intent.get("success") is True, "intent_probe_failed")
    _require(laws.get("success") is True, "law_probe_failed")
    _require(judgments.get("success") is True, "judgment_probe_failed")
    _require("Taiwan Law MCP" in direct_answer and "184" in direct_answer, "answer_integration_failed")

    judgment_items = [row for row in judgments.get("items") or [] if isinstance(row, dict)]
    _require(bool(judgment_items), "no_judgment_items")
    _require(all(row.get("source") == SOURCE for row in judgment_items), "wrong_judgment_source")
    _require(all(row.get("draft_eligible") is False for row in judgment_items), "remote_candidate_bypassed_gate")
    _require(all(row.get("verification_state") == "external_candidate" for row in judgment_items), "candidate_not_external")
    _require(all(str(row.get("jid") or "").strip() for row in judgment_items), "missing_official_jid")
    allowed_official_hosts = {"judgment.judicial.gov.tw", "cons.judicial.gov.tw", "law.moj.gov.tw"}
    observed_hosts = sorted({_official_host(row.get("source_url") or row.get("url")) for row in judgment_items})
    _require(all(host in allowed_official_hosts for host in observed_hosts), "unexpected_official_host")

    research_items = [row for row in research.get("items") or [] if isinstance(row, dict)]
    privacy = research.get("privacy") if isinstance(research.get("privacy"), dict) else {}
    _require(privacy.get("external_allowed") is True, "privacy_gate_not_recorded")

    evidence = {
        "schema_version": 1,
        "ok": True,
        "provider": SOURCE,
        "synthetic_only": True,
        "database_write": False,
        "checks": {
            "intent": {"ok": True, "tool": intent.get("tool")},
            "laws": {
                "ok": True,
                "tool": laws.get("tool"),
                "result_count": len(laws.get("results") or []),
                "payload_sha256": _digest(laws.get("results") or []),
            },
            "judgments": {
                "ok": True,
                "result_count": len(judgment_items),
                "jids": [str(row.get("jid")) for row in judgment_items],
                "official_hosts": observed_hosts,
                "payload_sha256": _digest(
                    [(row.get("jid"), row.get("source_url"), row.get("verification_state")) for row in judgment_items]
                ),
            },
            "answer": {"ok": True, "output_sha256": _digest(direct_answer)},
            "research": {
                "ok": bool(research.get("success")),
                "item_count": len(research_items),
                "remote_candidate_count": sum(
                    1 for row in research_items if row.get("source") == SOURCE
                ),
                "draft_eligible_remote_count": sum(
                    1 for row in research_items if row.get("source") == SOURCE and row.get("draft_eligible")
                ),
                "privacy_external_allowed": privacy.get("external_allowed"),
            },
        },
    }
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
