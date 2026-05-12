#!/usr/bin/env python3
"""Live smoke test for MAGI's Taiwan legal MCP adapter."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.domains import judgment_flow
from api.osc.taiwan_legal_mcp import (
    call_taiwan_legal_tool,
    search_practical_judgments_via_mcp,
    taiwan_legal_mcp_available,
    taiwan_legal_mcp_root,
)


def assert_ok(name: str, data: dict) -> dict:
    if not data.get("success") and not data.get("ok"):
        raise AssertionError(f"{name} failed: {data.get('error') or data}")
    return data


def main() -> int:
    root = taiwan_legal_mcp_root()
    if not taiwan_legal_mcp_available():
        raise SystemExit(f"taiwan legal MCP not installed: {root}; run scripts/setup_taiwan_legal_mcp.py")

    interp = assert_ok("get_interpretation", call_taiwan_legal_tool("get_interpretation", case_id="釋字748"))
    reg = assert_ok("query_regulation", call_taiwan_legal_tool("query_regulation", law_name="民法", article_no="184"))
    judgments = assert_ok(
        "search_practical_judgments_via_mcp",
        search_practical_judgments_via_mcp("預售屋 遲延交屋", case_type="民事", limit=2, fulltext_limit=1),
    )

    reply = judgment_flow.run_practical_insight_command(None, "實務見解 預售屋遲延交屋", notify=False)
    if "台灣法律資料庫 MCP" not in reply and "相關判決" not in reply:
        raise AssertionError("practical insight reply did not include judgment section")

    result = {
        "ok": True,
        "mcp_root": str(root),
        "interpretation": interp.get("case_id") or interp.get("number"),
        "regulation_articles": len(reg.get("articles") or []),
        "judgment_items": len(judgments.get("items") or []),
        "reply_preview": reply[:500],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

