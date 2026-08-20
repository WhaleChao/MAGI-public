from __future__ import annotations


def test_office_laf_criminal_count_is_deterministic_and_external_free(monkeypatch):
    from api.osc import utils
    from skills.engine import tool_registry

    seen = {}

    def fake_exec(sql, params=(), fetch="none"):
        seen["sql"] = sql
        seen["params"] = params
        seen["fetch"] = fetch
        return {"total": 63, "active_or_closing": 27, "final_closed": 36}, {"database": "law_firm_data"}

    monkeypatch.setattr(utils, "_osc_exec", fake_exec)
    reply = tool_registry._query_cases("請告訴我目前紀錄在案的法扶刑事案件數量，這個事務所的，不是其他資料")

    assert "共 63 件" in reply
    assert "27 件尚未最終結案" in reply
    assert "36 件已最終結案" in reply
    assert "僅查本所資料庫" in reply
    assert "legal_aid_number" in seen["sql"]
    assert "case_type" in seen["sql"]
    assert seen["params"] == ()
    assert seen["fetch"] == "one"


def test_office_case_count_scope_can_limit_to_active(monkeypatch):
    from api.osc import utils
    from skills.engine import tool_registry

    def fake_exec(sql, params=(), fetch="none"):
        assert "NOT" in sql
        return {"total": 27, "active_or_closing": 27, "final_closed": 0}, {}

    monkeypatch.setattr(utils, "_osc_exec", fake_exec)
    reply = tool_registry._query_cases("目前進行中的法扶刑事案件有幾件")

    assert "共 27 件" in reply
    assert "僅查本所資料庫" in reply


def test_all_scope_wins_when_followup_mentions_both_statuses(monkeypatch):
    from api.osc import utils
    from skills.engine import tool_registry

    def fake_exec(sql, params=(), fetch="none"):
        assert "WHERE NOT" not in " ".join(sql.split())
        return {"total": 63, "active_or_closing": 27, "final_closed": 36}, {}

    monkeypatch.setattr(utils, "_osc_exec", fake_exec)
    reply = tool_registry._query_cases(
        "本所法扶刑事案件有幾件（使用者補充：全部歷年紀錄，並區分尚未最終結案與已最終結案）"
    )

    assert "共 63 件" in reply
    assert "27 件尚未最終結案" in reply
    assert "36 件已最終結案" in reply


def test_case_statistics_take_direct_database_route(monkeypatch):
    from api.pipelines import message_pipeline
    from skills.engine import tool_registry

    class FakeOrchestrator:
        def __init__(self):
            self.traces = []

        def _append_route_trace(self, *args, **kwargs):
            self.traces.append((args, kwargs))

    monkeypatch.setattr(
        tool_registry,
        "_query_cases",
        lambda query: "事務所案件資料庫目前記錄的法扶刑事案件共 63 件。",
    )
    orch = FakeOrchestrator()
    reply = message_pipeline._maybe_direct_case_statistics(
        orch,
        "請告訴我本所全部歷年法扶刑事案件數量，不是其他資料",
        user_id="u1",
        platform="WEB",
    )

    assert "共 63 件" in reply
    assert any(args[3] == "direct_case_statistics" for args, _kwargs in orch.traces)


def test_case_statistics_policy_beats_generic_laf_route():
    from api.tools.policies import classify_tool_requirement

    routed = classify_tool_requirement(
        "請告訴我目前紀錄在案的法扶刑事案件數量，這個事務所的，不是其他資料",
        intent="QUERY",
    )

    assert routed.level == "required"
    assert routed.tool_hint == "db_query"
