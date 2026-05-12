from __future__ import annotations

from api.osc import taiwan_legal_mcp


def test_parse_taiwan_case_number_for_precise_judgment_search():
    parsed = taiwan_legal_mcp.parse_taiwan_case_number("114年度台上字第3753號")
    assert parsed == {
        "year_from": 114,
        "year_to": 114,
        "case_word": "台上",
        "case_number": "3753",
    }


def test_merge_judgment_sources_keeps_primary_and_adds_mcp():
    primary = {
        "success": True,
        "source_label": "本地實務見解庫",
        "items": [{"title": "本地", "url": "https://local/1"}],
    }
    supplemental = {
        "success": True,
        "source_label": "台灣法律資料庫 MCP（司法院公開資料）",
        "items": [{"title": "司法院", "url": "https://judgment/1"}],
    }
    merged = taiwan_legal_mcp.merge_judgment_sources(primary, supplemental, limit=3)
    assert len(merged["items"]) == 2
    assert "台灣法律資料庫 MCP" in merged["source_label"]


def test_disabled_tool_call_returns_soft_failure(monkeypatch):
    monkeypatch.setenv("MAGI_TAIWAN_LEGAL_MCP_ENABLE", "0")
    result = taiwan_legal_mcp.call_taiwan_legal_tool("get_interpretation", case_id="釋字748")
    assert result["success"] is False
    assert result["error"] == "taiwan_legal_mcp_disabled"

