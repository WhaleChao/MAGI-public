from __future__ import annotations


def test_normalize_case_query_extracts_magi_case_number():
    from skills.engine.tool_registry import _normalize_case_query

    assert _normalize_case_query("請查2025-0049案件") == "2025-0049"
    assert _normalize_case_query("麻煩幫我找 2026-0048 案件資料") == "2026-0048"


def test_query_cases_accepts_natural_language_case_lookup(monkeypatch):
    import api.osc.utils as osc_utils
    from skills.engine.tool_registry import _query_cases

    def fake_exec(sql, params=(), fetch="all"):
        assert fetch == "all"
        assert "%2025-0049%" in params
        return (
            [
                {
                    "case_number": "2025-0049",
                    "client_name": "林洋宇",
                    "case_reason": "更生",
                    "court_case_no": "114年度司執消債更字第000442號",
                    "status": "進行中",
                }
            ],
            None,
        )

    monkeypatch.setattr(osc_utils, "_osc_exec", fake_exec)

    result = _query_cases("請查2025-0049案件")

    assert "林洋宇" in result
    assert "2025-0049" in result


def test_message_pipeline_direct_case_lookup_bypasses_llm(monkeypatch):
    from api.pipelines import message_pipeline
    import skills.engine.tool_registry as tool_registry

    class FakeOrch:
        traces = []

        def _append_route_trace(self, *args, **kwargs):
            self.traces.append((args, kwargs))

    monkeypatch.setattr(tool_registry, "_query_cases", lambda query: "- 林洋宇 | 2025-0049 | 更生 | 狀態: 進行中")

    reply = message_pipeline._maybe_direct_case_lookup(FakeOrch(), "請查2025-0049案件")

    assert "林洋宇" in reply
    assert "2025-0049" in reply


def test_message_pipeline_direct_case_lookup_does_not_capture_mutations(monkeypatch):
    from api.pipelines import message_pipeline
    import skills.engine.tool_registry as tool_registry

    called = {"query": False}

    def fake_query(_query):
        called["query"] = True
        return "should not be called"

    monkeypatch.setattr(tool_registry, "_query_cases", fake_query)

    reply = message_pipeline._maybe_direct_case_lookup(object(), "2026-0048已經修正成另一案")

    assert reply == ""
    assert called["query"] is False
