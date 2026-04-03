import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from skills.bridge import grounded_ai
from skills.memory import mem_bridge


def test_auto_remember_disabled_by_default(monkeypatch):
    called = []

    monkeypatch.setattr(grounded_ai, "ENABLE_AUTO_MEMORIZE", False)
    monkeypatch.setattr(grounded_ai, "remember", lambda *args, **kwargs: called.append((args, kwargs)))

    grounded_ai._auto_remember("使用者問題", "這是一段足夠長的回答內容", mode="chat")
    assert called == []


def test_auto_remember_requires_explicit_mode(monkeypatch):
    called = []

    monkeypatch.setattr(grounded_ai, "ENABLE_AUTO_MEMORIZE", True)
    monkeypatch.setattr(grounded_ai, "remember", lambda *args, **kwargs: called.append((args, kwargs)))

    grounded_ai._auto_remember("使用者問題", "這是一段足夠長的回答內容", mode="chat")
    grounded_ai._auto_remember("使用者問題", "這是一段足夠長的回答內容", mode="ask")
    assert called == []


def test_memory_ranking_prioritizes_trusted_sources_for_fact_queries():
    items = [
        {"content": "chatlog answer", "source": "chatlog|platform=Discord|user=1", "score": 0.99},
        {"content": "assistant summary", "source": "assistant_generated|mode=chat|ts=20260402_1200", "score": 0.95},
        {"content": "confirmed rule", "source": "user_rule|platform=LINE|user=2", "score": 0.40},
        {"content": "profile fact", "source": "user_profile_3", "score": 0.55},
    ]

    ranked = mem_bridge._rank_recall_results("請回答這個事實", items)
    assert ranked[0]["source"] in {"user_rule|platform=LINE|user=2", "user_profile_3"}
    assert ranked[-1]["source"].startswith("assistant_generated") or ranked[-1]["source"].startswith("chatlog|")
    assert mem_bridge._source_trust_weight("user_rule|platform=LINE|user=2") > mem_bridge._source_trust_weight("chatlog|platform=Discord|user=1")
    assert mem_bridge._source_trust_weight("chatlog|platform=Discord|user=1") > mem_bridge._source_trust_weight("assistant_generated|mode=chat")


def test_memory_ranking_keeps_chatlog_available_for_explicit_recall():
    items = [
        {"content": "chatlog answer", "source": "chatlog|platform=Discord|user=1", "score": 0.99},
        {"content": "confirmed rule", "source": "user_rule|platform=LINE|user=2", "score": 0.40},
        {"content": "assistant summary", "source": "assistant_generated|mode=chat|ts=20260402_1200", "score": 0.20},
    ]

    ranked = mem_bridge._rank_recall_results("你還記得我之前說過什麼嗎", items)
    assert ranked[0]["source"].startswith("chatlog|")
    assert ranked[0]["trust_weight"] > ranked[-1]["trust_weight"]


def test_expand_query_skips_memory_recall_queries(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "banana\nirrelevant expansion"
                        }
                    }
                ]
            }

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(mem_bridge, "ENABLE_QUERY_EXPANSION", True)
    monkeypatch.setattr(mem_bridge, "_get_session", lambda: FakeSession())

    query = "你還記得我之前說過什麼嗎"
    assert mem_bridge.expand_query(query) == [query]


def test_expand_query_filters_far_variations(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "banana\n法院判決摘要重點"
                        }
                    }
                ]
            }

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(mem_bridge, "ENABLE_QUERY_EXPANSION", True)
    monkeypatch.setattr(mem_bridge, "_get_session", lambda: FakeSession())

    query = "請幫我找最新法院判決摘要重點"
    expanded = mem_bridge.expand_query(query)
    assert expanded[0] == query
    assert "法院判決摘要重點" in expanded
    assert "banana" not in expanded
