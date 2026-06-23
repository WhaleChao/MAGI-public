from api.routing.intent_contract import (
    KIND_AGENT_TASK,
    KIND_CASUAL_CHAT,
    KIND_EXPLICIT_TASK,
    KIND_HELP_COMMAND,
    KIND_META_CAPABILITY,
    KIND_REALTIME_ACTION,
    KIND_STATEFUL_REPLY,
    KIND_TOOL_CAPABILITY,
    KIND_UNKNOWN,
    classify_intent_contract,
    looks_like_agentic_request,
)


def _kind(message: str) -> str:
    return classify_intent_contract(message).kind


def test_help_and_capability_are_distinct():
    assert _kind("/help") == KIND_HELP_COMMAND
    assert _kind("功能列表") == KIND_HELP_COMMAND
    assert _kind("你現在是什麼模型可以做什麼事情") == KIND_META_CAPABILITY
    assert _kind("請問你能做到什麼事") == KIND_META_CAPABILITY
    assert _kind("有什麼功能") in {KIND_HELP_COMMAND, KIND_META_CAPABILITY}


def test_tool_capability_does_not_execute_realtime_but_concrete_query_does(monkeypatch):
    assert _kind("你可以查天氣嗎？") == KIND_TOOL_CAPABILITY
    assert _kind("MAGI 能不能查匯率？") == KIND_TOOL_CAPABILITY

    monkeypatch.setattr(
        "api.routing.intent_contract.classify_realtime_kind",
        lambda message: "weather" if "天氣" in message else "",
    )
    decision = classify_intent_contract("那你能查一下明天台北的天氣嗎")
    assert decision.kind == KIND_REALTIME_ACTION
    assert decision.realtime_kind == "weather"


def test_chat_boundaries_bypass_state_but_form_answers_do_not():
    casual = classify_intent_contract("我只是想跟你聊聊天")
    assert casual.kind == KIND_CASUAL_CHAT
    assert casual.bypass_state is True

    amount = classify_intent_contract("50000")
    assert amount.kind == KIND_STATEFUL_REPLY
    assert amount.bypass_state is False

    court = classify_intent_contract("臺灣新北地方法院")
    assert court.kind == KIND_STATEFUL_REPLY
    assert court.bypass_state is False


def test_new_document_task_is_not_treated_as_short_form_reply():
    decision = classify_intent_contract("幫我做委任狀")
    assert decision.kind == KIND_EXPLICIT_TASK
    assert decision.bypass_state is True

    name = classify_intent_contract("王小明")
    assert name.kind == KIND_UNKNOWN
    assert name.bypass_state is False


def test_broad_analysis_and_tool_requests_route_to_agent():
    decision = classify_intent_contract("請幫我比較民法184條與相關判決見解")
    assert decision.kind == KIND_AGENT_TASK
    assert decision.bypass_state is True
    assert _kind("請幫我比較民法184條與相關判決見解，整理成三點") == KIND_AGENT_TASK
    assert _kind("請幫我查法規並整理重點") == KIND_AGENT_TASK
    assert looks_like_agentic_request("我想知道這份文件的重點與風險")


def test_agentic_request_excludes_short_replies_and_write_workflows():
    assert classify_intent_contract("50000").kind == KIND_STATEFUL_REPLY
    assert classify_intent_contract("王小明").kind == KIND_UNKNOWN
    assert classify_intent_contract("查案件 2025-0134").kind == KIND_EXPLICIT_TASK
    assert classify_intent_contract("幫我做委任狀").kind == KIND_EXPLICIT_TASK
    assert not looks_like_agentic_request("請幫我刪除這個檔案")
