from __future__ import annotations

import pytest

from api.pipelines.message_pipeline import _agentic_grounding_issue
from api.routing.clarification import detect_clarification_need
from api.routing.intent_contract import KIND_TOOL_CAPABILITY, classify_intent_contract
from api.tools.policies import classify_tool_requirement
from skills.engine.realtime_data_gateway import classify_realtime_query, detect_realtime_topics
from skills.engine.tool_registry import TOOLS


PLACES = [
    "臺北", "台北", "新北", "桃園", "臺中", "高雄",
    "花蓮", "臺東", "東京", "大阪", "首爾", "紐約",
]
WEATHER_FORMS = [
    "明天{place}天氣如何",
    "請查{place}後天會不會下雨",
    "告訴我{place}現在幾度",
]
WEATHER_ACTIONS = [form.format(place=place) for place in PLACES for form in WEATHER_FORMS]


@pytest.mark.parametrize("message", WEATHER_ACTIONS)
def test_explicit_weather_actions_require_authoritative_realtime(message):
    assert classify_realtime_query(message) == "weather"
    requirement = classify_tool_requirement(message, intent="QUERY", has_memory_context=True)
    assert (requirement.level, requirement.tool_hint) == ("required", "realtime_lookup")


WEATHER_NON_ACTIONS = [
    "解釋天氣形成的原因",
    "天氣和氣候有什麼差別",
    "把『明天台北天氣如何』翻成英文",
    "摘要這句話：明天台北天氣如何",
    "不要查天氣，說個笑話",
    "我沒有問天氣",
    "如果明天下雨，租約條款應如何寫",
    "假設明天下雪，法院會不會停止上班",
    "去年今天台北天氣如何",
    "2020年8月臺北的天氣紀錄",
    "天氣 API 是什麼",
    "檢查天氣模組為何報錯",
    "『會不會下雨』是疑問句嗎",
    "將 weather forecast 翻譯成中文",
    "我剛才說的天氣問題不用查了",
    "不要使用工具，解釋颱風如何形成",
]


@pytest.mark.parametrize("message", WEATHER_NON_ACTIONS)
def test_weather_words_without_current_lookup_do_not_trigger_realtime(message):
    assert classify_realtime_query(message) is None
    requirement = classify_tool_requirement(message, intent="CHAT")
    assert requirement.tool_hint != "realtime_lookup"


STOCK_ACTIONS = [
    f"目前{symbol}股價多少"
    for symbol in ("台積電", "鴻海", "2330", "2317", "0050", "大盤")
] + [
    "請查2330現在成交價", "告訴我台股今天加權指數", "目前台積電一股多少錢",
]


@pytest.mark.parametrize("message", STOCK_ACTIONS)
def test_stock_actions_require_realtime(message):
    assert classify_realtime_query(message) == "stock"
    requirement = classify_tool_requirement(message, intent="QUERY", has_memory_context=True)
    assert (requirement.level, requirement.tool_hint) == ("required", "realtime_lookup")


STOCK_NON_ACTIONS = [
    "股票是什麼",
    "解釋股價形成機制",
    "把 stock price 翻成中文",
    "不要查股價",
    "檢查股票模組為何故障",
    "假設台積電股價下跌，契約如何處理",
    "我的心情跌到谷底",
    "這個產品上市了嗎",
    "物價漲了嗎",
    "移除股票追蹤清單",
    "設定台積電晨報",
    "分析 RSI 與 MACD 的差異",
]


@pytest.mark.parametrize("message", STOCK_NON_ACTIONS)
def test_stock_vocabulary_without_quote_request_does_not_trigger_realtime(message):
    assert classify_realtime_query(message) is None
    assert classify_tool_requirement(message, intent="CHAT").tool_hint != "realtime_lookup"


FX_ACTIONS = [
    f"目前{currency}匯率多少"
    for currency in ("美元", "美金", "日圓", "日幣", "歐元", "人民幣", "港幣")
] + ["請查現在 USD/TWD 匯率", "今天一美元換多少臺幣"]


@pytest.mark.parametrize("message", FX_ACTIONS)
def test_fx_actions_require_realtime(message):
    assert classify_realtime_query(message) == "fx_rate"
    requirement = classify_tool_requirement(message, intent="QUERY", has_memory_context=True)
    assert (requirement.level, requirement.tool_hint) == ("required", "realtime_lookup")


FX_NON_ACTIONS = [
    "匯率是什麼",
    "解釋浮動匯率制度",
    "把 exchange rate 翻成中文",
    "不要查匯率",
    "檢查匯率模組為何報錯",
    "假設美元升值，契約如何約定",
    "外幣債權如何聲請強制執行",
    "摘要這句：目前美元匯率多少",
]


@pytest.mark.parametrize("message", FX_NON_ACTIONS)
def test_fx_vocabulary_without_quote_request_does_not_trigger_realtime(message):
    assert classify_realtime_query(message) is None
    assert classify_tool_requirement(message, intent="CHAT").tool_hint != "realtime_lookup"


TIME_ACTIONS = [
    "現在幾點", "目前時間", "此刻時間", "現在日期", "目前是幾月幾日",
    "請告訴我現在幾點", "臺灣現在幾點", "現在是上午還下午",
]


@pytest.mark.parametrize("message", TIME_ACTIONS)
def test_current_time_actions_use_deterministic_clock(message):
    assert classify_realtime_query(message) == "current_time"
    requirement = classify_tool_requirement(message, intent="QUERY", has_memory_context=True)
    assert (requirement.level, requirement.tool_hint) == ("required", "current_time")


TIME_NON_ACTIONS = [
    "解釋時間管理方法",
    "目前時間管理出了什麼問題",
    "把『現在幾點』翻成英文",
    "我不是問現在幾點",
    "不要告訴我現在時間",
    "案件發生時間是何時",
    "文件上的日期是什麼",
    "分析現在與過去的差別",
]


@pytest.mark.parametrize("message", TIME_NON_ACTIONS)
def test_time_vocabulary_without_clock_request_does_not_trigger_clock(message):
    assert classify_realtime_query(message) is None
    assert classify_tool_requirement(message, intent="CHAT").tool_hint != "current_time"


CAPABILITY_QUESTIONS = [
    "你會查天氣嗎", "MAGI 能查股價嗎", "你可以查匯率嗎", "你能不能搜尋新聞",
    "你會讀案件資料嗎", "你能建立行程嗎", "你可以下載卷宗嗎", "MAGI 會寫書狀嗎",
]


@pytest.mark.parametrize("message", CAPABILITY_QUESTIONS)
def test_capability_questions_do_not_execute_tools(message):
    assert classify_intent_contract(message).kind == KIND_TOOL_CAPABILITY
    assert classify_tool_requirement(message, intent="CHAT").level == "none"


AMBIGUOUS_MUTATIONS = [
    "刪除這個案件", "移除那份檔案", "上傳這個", "搬移那份資料",
    "修改這筆紀錄", "更新當事人姓名", "更正案號", "取消那個行程",
]


@pytest.mark.parametrize("message", AMBIGUOUS_MUTATIONS)
def test_ambiguous_mutations_require_clarification(message):
    assert detect_clarification_need(message).needed is True


COMPOUND_CASES = [
    ("查明天台北天氣並列出今天行程", "realtime_lookup", "get_schedule", "realtime", "calendar"),
    ("查最新國際新聞並列出今天行程", "web_search", "get_schedule", "web_current", "calendar"),
    ("查2026-0062案件並列出下次庭期", "query_cases", "get_schedule", "case", "calendar"),
    ("找民法第184條與相關最高法院見解", "search_statutes", "search_judgments", "statute", "judgment"),
]


@pytest.mark.parametrize("message,tool_a,tool_b,missing_a,missing_b", COMPOUND_CASES)
def test_compound_requests_require_all_authoritative_sources(message, tool_a, tool_b, missing_a, missing_b):
    answer = "整理結果如下。"
    assert missing_a in _agentic_grounding_issue(message, answer, {"tools_used": [tool_b]})
    assert missing_b in _agentic_grounding_issue(message, answer, {"tools_used": [tool_a]})
    assert _agentic_grounding_issue(message, answer, {"tools_used": [tool_a, tool_b]}) == ""


GENERAL_CHAT = [
    "你好", "今天心情不好", "陪我聊聊", "你覺得人生的意義是什麼",
    "講個笑話", "幫我想午餐吃什麼", "這段話寫得自然嗎", "謝謝你",
    "你剛才的說明很清楚", "我有點累", "晚安", "早安",
]


@pytest.mark.parametrize("message", GENERAL_CHAT)
def test_general_chat_does_not_force_authoritative_tools(message):
    requirement = classify_tool_requirement(message, intent="CHAT")
    assert requirement.level == "none"
    assert detect_realtime_topics(message) == set()


def test_realtime_tool_success_never_exposes_internal_control_marker(monkeypatch):
    monkeypatch.setattr(
        "skills.engine.realtime_data_gateway.handle_realtime_query",
        lambda _query: {"success": True, "reply": "臺北目前 30°C。"},
    )
    answer = TOOLS["realtime_lookup"]["fn"](query="臺北天氣")
    assert answer == "臺北目前 30°C。"
    assert "VERIFIED_REALTIME" not in answer


def test_realtime_tool_exception_is_fail_closed_without_exception_name(monkeypatch):
    def fail(_query):
        raise RuntimeError("secret backend detail")

    monkeypatch.setattr("skills.engine.realtime_data_gateway.handle_realtime_query", fail)
    answer = TOOLS["realtime_lookup"]["fn"](query="臺北天氣")
    assert answer.startswith("[REALTIME_UNAVAILABLE]")
    assert "RuntimeError" not in answer
    assert "secret backend detail" not in answer
