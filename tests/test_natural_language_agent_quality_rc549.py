from __future__ import annotations

import json

from api.pipelines.message_pipeline import (
    _agentic_grounding_issue,
    _looks_like_payment_slip_scan_request,
    _verified_source_decline,
)
from api.routing.clarification import detect_clarification_need
from api.routing.intent_contract import (
    KIND_AGENT_TASK,
    KIND_EXPLICIT_TASK,
    classify_intent_contract,
)
from api.routing.office_cognition import assess_office_request
from api.tools.policies import classify_tool_requirement
from skills.engine.react_engine import ReActEngine
from skills.engine.realtime_data_gateway import classify_realtime_query
from skills.engine.tool_registry import TOOLS


def test_calendar_mutation_is_not_stolen_by_weather_or_stock_routes():
    message = "把明天下午三點的庭改到四點"
    assert classify_realtime_query(message) is None
    assert classify_intent_contract(message).kind == KIND_EXPLICIT_TASK
    assert "calendar" in {item.name for item in assess_office_request(message).candidates}

    assert classify_realtime_query("新北地院民事股的庭期") is None
    assert classify_realtime_query("明天台北天氣如何") == "weather"
    assert classify_realtime_query("台積電股價") == "stock"


def test_natural_office_language_reaches_work_or_agent_lanes():
    expected = {
        "查一下最近收到的法扶結案酬金通知並下載附件": KIND_EXPLICIT_TASK,
        "檢查所有新信件，有需要就建案或下載附件": KIND_EXPLICIT_TASK,
        "把今天漏掉的繳費單抓回來並通知我": KIND_EXPLICIT_TASK,
        "搜尋侵權行為損害賠償最新實務並列出可引用段落": KIND_AGENT_TASK,
        "曾昌義那件期限是什麼？把來源文件也給我": KIND_AGENT_TASK,
        "查看系統紅燈並能自動修的就修好": KIND_EXPLICIT_TASK,
    }
    for message, kind in expected.items():
        assert classify_intent_contract(message).kind == kind, message


def test_human_like_clarification_for_vague_or_multiple_targets():
    generic = detect_clarification_need("這個幫我處理一下")
    assert generic.needed is True
    assert generic.key == "work_target"

    multiple = detect_clarification_need("吳小姐那案有兩件，幫我下載最新卷宗")
    assert multiple.needed is True
    assert multiple.key == "case_disambiguation"

    meridiem = detect_clarification_need("把明天三點的庭改到四點")
    assert meridiem.needed is True
    assert meridiem.key == "schedule_meridiem"


def test_tool_policy_understands_natural_translation_and_business_status():
    translated = classify_tool_requirement("剛剛那份翻成英文，保留法律術語", intent="QUERY")
    assert translated.level == "required"
    assert translated.tool_hint == "document_processing"

    business = classify_tool_requirement("幫我確認閱卷模組有沒有漏下載", intent="QUERY")
    assert business.level == "required"
    assert business.tool_hint == "business_status"

    latest_bundle = classify_tool_requirement("吳小姐那案幫我下載最新卷宗", intent="QUERY")
    assert latest_bundle.level == "required"
    assert latest_bundle.tool_hint == "file_review_query"

    deadline_source = classify_tool_requirement("曾昌義那件期限是什麼？把來源文件也給我", intent="QUERY")
    assert deadline_source.level == "required"
    assert deadline_source.tool_hint == "todo_query"


def test_controlled_evolution_questions_use_read_only_ledger_tool():
    for message in (
        "MAGI 目前哪裡需要進化？",
        "你有哪些待驗證修補候選？",
        "查看自我演化狀態",
    ):
        requirement = classify_tool_requirement(message, intent="QUERY")
        assert requirement.level == "required"
        assert requirement.tool_hint == "evolution_status"
        office = assess_office_request(message)
        assert office.tool_requirement.tool_hint == "evolution_status"

    # Asking whether a feature exists is still ordinary capability dialogue;
    # it must not fabricate a backlog query or start a mutation.
    capability = classify_tool_requirement("你可以進行受控自我演化嗎？", intent="QUERY")
    assert capability.tool_hint != "evolution_status"


def test_evolution_grounding_requires_the_specific_ledger_tool():
    message = "MAGI 目前哪裡需要進化？"
    answer = "目前有一個待驗證候選。"
    assert (
        _agentic_grounding_issue(message, answer, {"tools_used": ["system_health"]})
        == "required_tool_coverage_missing:evolution"
    )
    assert _agentic_grounding_issue(message, answer, {"tools_used": ["evolution_status"]}) == ""


def test_payment_slip_recovery_requires_explicit_action_and_reaches_tool_policy():
    command = "把今天漏掉的繳費單抓回來並通知我"
    assert _looks_like_payment_slip_scan_request(command) is True
    assert _looks_like_payment_slip_scan_request("為什麼繳費單會漏掉？") is False
    assert _looks_like_payment_slip_scan_request("繳費單的下載機制是什麼？") is False

    requirement = classify_tool_requirement(command, intent="QUERY")
    assert requirement.level == "required"
    assert requirement.tool_hint == "business_status"


def test_declining_tools_never_downgrades_current_office_facts_to_chat():
    reply, domain, operation = _verified_source_decline("不要用工具，告訴我本所目前刑事法扶案件數")
    assert "不會猜測" in reply
    assert domain in {"case", "legal_aid"}
    assert operation == "lookup"

    assert _verified_source_decline("不要用工具，解釋什麼是比例原則") == ("", "", "")


def test_compound_agent_answer_requires_each_authoritative_tool():
    message = "先查案件資料再列出下週庭期和未完成期限"
    answer = "我已查完並整理如下。"
    assert _agentic_grounding_issue(message, answer, {"tools_used": ["query_cases"]}) == "required_tool_coverage_missing:calendar"
    assert _agentic_grounding_issue(
        message,
        answer,
        {"tools_used": ["query_cases", "get_schedule"]},
    ) == ""


def test_react_rejects_unknown_or_missing_parameters_before_executor():
    calls = []

    def executor(query: str):
        calls.append(query)
        return "ok"

    tools = {
        "lookup": {
            "fn": executor,
            "desc": "synthetic",
            "params": "query: str",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    }
    engine = ReActEngine(tools=tools, llm_fn=lambda _: "FINAL: unused")
    assert "missing_required" in (engine._iron_dome_check("lookup", {}) or "")
    assert "unexpected_property" in (engine._iron_dome_check("lookup", {"query": "x", "secret": "y"}) or "")
    assert calls == []


def test_privacy_safe_health_tools_read_only_aggregate_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "function_health_index_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "generated_at": "2026-08-15T00:00:00+00:00",
                "summary": {
                    "failed_health_count": 0,
                    "stale_health_count": 0,
                    "missing_health_count": 0,
                    "pending_occurrence_count": 2,
                },
                "sensitive_case": "must-not-leak",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "business_module_live_check_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "generated_at": "2026-08-15T00:00:00+00:00",
                "release_id": "synthetic-release",
                "results": [
                    {"name": "file_review_self_test", "ok": True, "parsed": {"party": "must-not-leak"}},
                    {"name": "laf_portal_live", "ok": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    health = TOOLS["system_health"]["fn"]()
    review = TOOLS["business_status"]["fn"](module="閱卷")
    assert "must-not-leak" not in health
    assert "must-not-leak" not in review
    assert "file_review_self_test" in review
    assert "laf_portal_live" not in review
