"""Tests for the Tool-First Factual Pipeline (Batch 2B)."""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.tools.policies import classify_tool_requirement, format_tool_failure_response
from api.tools.tool_router import route_to_tool, ToolRouteResult


# ── classify_tool_requirement ──


def test_case_query_required():
    req = classify_tool_requirement("查一下112年度訴字第1234號案件的進度")
    assert req.level == "required"
    assert req.tool_hint == "case_query"


def test_calendar_query_required():
    req = classify_tool_requirement("明天有什麼行程")
    assert req.level == "required"
    assert req.tool_hint == "calendar_query"


def test_schedule_today_required():
    req = classify_tool_requirement("今天開庭嗎")
    assert req.level == "required"
    assert req.tool_hint == "calendar_query"


def test_laf_query_required():
    req = classify_tool_requirement("法扶未開辦案件狀態")
    assert req.level == "required"
    assert req.tool_hint in {"laf_query", "case_query"}


def test_report_query_required():
    req = classify_tool_requirement("看一下今天的晨報內容")
    assert req.level == "required"
    assert req.tool_hint == "report_query"


def test_db_query_required():
    req = classify_tool_requirement("資料庫裡有多少筆案件")
    assert req.level == "required"
    assert req.tool_hint == "db_query"


def test_legal_reference_required_no_memory():
    req = classify_tool_requirement("民法第184條的內容", has_memory_context=False)
    assert req.level == "required"
    assert req.tool_hint == "legal_reference"


def test_legal_reference_optional_with_memory():
    req = classify_tool_requirement("民法第184條的內容", has_memory_context=True)
    assert req.level == "optional"
    assert req.tool_hint == "legal_reference"


def test_judgment_query():
    req = classify_tool_requirement("最高法院有什麼判決見解")
    assert req.tool_hint == "judgment_query"


def test_web_research():
    req = classify_tool_requirement("今天台北天氣如何")
    assert req.tool_hint == "web_research"


def test_general_chat_no_tool():
    req = classify_tool_requirement("你好嗎")
    assert req.level == "none"
    assert req.tool_hint == ""


def test_query_intent_defaults_optional():
    req = classify_tool_requirement("這是什麼意思", intent="QUERY")
    assert req.level == "optional"


def test_empty_message():
    req = classify_tool_requirement("")
    assert req.level == "none"


# ── format_tool_failure_response ──


def test_failure_response_with_error():
    resp = format_tool_failure_response("case_query", "connection timeout")
    assert "案件查詢" in resp
    assert "connection timeout" in resp


def test_failure_response_no_error():
    resp = format_tool_failure_response("calendar_query")
    assert "行程查詢" in resp
    assert "猜測" in resp


def test_failure_response_unknown_tool():
    resp = format_tool_failure_response("unknown_tool")
    assert "查詢工具" in resp


# ── route_to_tool ──


def test_route_to_tool_required():
    result = route_to_tool("查一下案件進度", intent="CMD")
    assert result.used_tool is True
    assert result.requirement_level == "required"
    assert result.failure_response  # pre-generated


def test_route_to_tool_none():
    result = route_to_tool("你好嗎", intent="CHAT")
    assert result.used_tool is False
    assert result.requirement_level == "none"


def test_route_result_as_context_empty():
    result = ToolRouteResult()
    assert result.as_context() == ""


def test_route_result_as_context_success():
    result = ToolRouteResult(
        used_tool=True,
        tool_hint="case_query",
        success=True,
        structured_output="案件 112訴1234 狀態：已結案",
    )
    ctx = result.as_context()
    assert "case_query" in ctx
    assert "已結案" in ctx


def test_route_result_as_context_failure():
    result = ToolRouteResult(
        used_tool=True,
        tool_hint="calendar_query",
        success=False,
        error="Google Calendar API 逾時",
    )
    ctx = result.as_context()
    assert "失敗" in ctx
    assert "逾時" in ctx
