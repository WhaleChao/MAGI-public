from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from api.pipelines.message_pipeline import _agentic_grounding_issue, _try_tool_first_policy_route
from api.routing.intent_contract import (
    KIND_AGENT_TASK,
    KIND_EXPLICIT_TASK,
    KIND_REALTIME_ACTION,
    KIND_TOOL_CAPABILITY,
    classify_intent_contract,
)
from api.tools.policies import classify_tool_requirement
from skills.engine.realtime_data_gateway import (
    _query_open_meteo,
    classify_realtime_query,
    detect_realtime_topics,
    query_weather,
)
from skills.engine.tool_registry import TOOLS


@pytest.mark.parametrize(
    "message",
    [
        "明天吃什麼？",
        "我的心情跌到谷底",
        "這個產品上市了嗎",
        "物價漲了嗎",
        "把明天下午三點的庭改到四點",
    ],
)
def test_keywords_do_not_steal_unrelated_questions(message):
    assert classify_realtime_query(message) is None


def test_realtime_and_capability_intents_are_distinct():
    assert classify_intent_contract("明天台北天氣如何").kind == KIND_REALTIME_ACTION
    assert classify_intent_contract("東京明天天氣如何").kind == KIND_REALTIME_ACTION
    assert classify_intent_contract("現在幾點").kind == KIND_REALTIME_ACTION
    assert classify_intent_contract("你會查天氣嗎").kind == KIND_TOOL_CAPABILITY
    assert classify_intent_contract("把明天下午三點的庭改到四點").kind == KIND_EXPLICIT_TASK


def test_missing_weather_location_asks_smallest_question_instead_of_guessing():
    result = query_weather("今天天氣如何")
    assert result["success"] is False
    assert "哪個地點" in result["refusal"]
    assert "請直接查閱" not in result["refusal"]


def test_current_facts_stay_required_even_with_memory():
    cases = {
        "明天台北天氣如何": "realtime_lookup",
        "目前台積電股價": "realtime_lookup",
        "現在幾點": "current_time",
        "今天最新國際新聞": "web_research",
        "目前中華民國總統是誰": "web_research",
        "今晚球賽最新比分": "web_research",
    }
    for message, expected_tool in cases.items():
        requirement = classify_tool_requirement(message, intent="QUERY", has_memory_context=True)
        assert requirement.level == "required"
        assert requirement.tool_hint == expected_tool


def test_compound_request_requires_every_authoritative_source():
    message = "查明天台北天氣並列出我的行程"
    assert classify_intent_contract(message).kind == KIND_AGENT_TASK
    assert detect_realtime_topics(message) == {"weather"}
    answer = "整理結果如下。"
    assert _agentic_grounding_issue(
        message, answer, {"tools_used": ["get_schedule"]}
    ) == "required_tool_coverage_missing:realtime"
    assert _agentic_grounding_issue(
        message, answer, {"tools_used": ["realtime_lookup"]}
    ) == "required_tool_coverage_missing:calendar"
    assert _agentic_grounding_issue(
        message, answer, {"tools_used": ["realtime_lookup", "get_schedule"]}
    ) == ""


def test_compound_current_news_cannot_be_grounded_by_unrelated_tool():
    message = "查今天最新國際新聞，再列出我的行程"
    answer = "整理結果如下。"
    assert _agentic_grounding_issue(
        message, answer, {"tools_used": ["get_schedule"]}
    ) == "required_tool_coverage_missing:web_current"
    assert _agentic_grounding_issue(
        message, answer, {"tools_used": ["web_search", "get_schedule"]}
    ) == ""


def test_react_registry_has_authoritative_realtime_tool():
    tool = TOOLS["realtime_lookup"]
    assert tool["side_effect"] == "read_only"
    assert tool["input_schema"]["required"] == ["query"]
    assert "權威" in tool["desc"]


def test_react_coerces_generic_weather_search_to_authoritative_source():
    from skills.engine.react_engine import ReActEngine

    engine = ReActEngine(tools=TOOLS, llm_fn=lambda _messages: "FINAL: unused")
    name, params, reason = engine._coerce_action_for_query(
        "東京明天天氣如何", "web_search", {"query": "東京天氣"}
    )
    assert name == "realtime_lookup"
    assert params == {"query": "東京明天天氣如何"}
    assert reason == "coerced_authoritative_realtime_source"


def test_react_returns_authoritative_failure_verbatim_without_second_model_guess():
    from skills.engine.react_engine import ReActEngine

    calls = []

    def llm(_messages):
        calls.append("llm")
        return 'ACTION: realtime_lookup\nPARAMS: {"query": "今天天氣如何"}'

    tools = {
        "realtime_lookup": {
            "fn": lambda **_kwargs: "[REALTIME_UNAVAILABLE] 你想查哪個地點的天氣？",
            "desc": "synthetic",
            "params": "query: str",
            "side_effect": "read_only",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    }
    result = ReActEngine(tools=tools, llm_fn=llm).run("今天天氣如何")
    assert result["answer"] == "你想查哪個地點的天氣？"
    assert result["tool_result_missing"] is True
    assert calls == ["llm"]


def test_tool_first_fast_path_never_truncates_compound_sources(monkeypatch):
    called = []
    original = TOOLS["realtime_lookup"]["fn"]
    monkeypatch.setitem(TOOLS["realtime_lookup"], "fn", lambda **_kwargs: called.append("realtime") or "bad")
    try:
        assert _try_tool_first_policy_route(
            object(),
            "查明天台北天氣並列出我的行程",
            intent="QUERY",
        ) == ""
        assert called == []
    finally:
        TOOLS["realtime_lookup"]["fn"] = original


def test_open_meteo_formats_provider_data_without_model_synthesis(monkeypatch):
    payloads = [
        {
            "results": [
                {
                    "name": "東京",
                    "admin1": "東京都",
                    "country": "日本",
                    "latitude": 35.6895,
                    "longitude": 139.6917,
                }
            ]
        },
        {
            "current": {
                "temperature_2m": 28.1,
                "apparent_temperature": 30.0,
                "precipitation": 0.0,
            },
            "daily": {
                "time": ["2026-08-15", "2026-08-16", "2026-08-17"],
                "weather_code": [1, 61, 3],
                "temperature_2m_max": [31.0, 29.0, 28.0],
                "temperature_2m_min": [24.0, 23.0, 22.0],
                "precipitation_probability_max": [10, 70, 20],
            },
        },
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert timeout == 8
        parsed = urlparse(request.full_url)
        assert parsed.scheme == "https"
        return Response(payloads.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = _query_open_meteo("東京", "東京明天天氣如何")
    assert result["success"] is True
    assert result["location"] == "東京、日本"
    assert "明天（2026-08-16）：小雨，23.0～29.0°C，最高降雨機率 70%" in result["reply"]
    assert "Open-Meteo" in result["reply"]
