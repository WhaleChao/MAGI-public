from api.pipelines.skill_dispatch import dispatch_case_management


def test_chat_case_creation_strips_suspected_marker(monkeypatch):
    inserted = {}

    def fake_exec(sql, params=(), fetch="none"):
        if sql.startswith("INSERT INTO cases"):
            inserted["params"] = params
            return {"rowcount": 1}, {"host": "127.0.0.1"}
        return None, {"host": "127.0.0.1"}

    monkeypatch.setattr("api.osc.utils._osc_exec", fake_exec)
    monkeypatch.setattr("api.osc.utils._osc_resolve_case_id", lambda *args, **kwargs: "")

    message = dispatch_case_management("建案 2026-0071 李滿金 刑事 涉詐欺、洗錢防制法")

    assert message is not None
    assert "案由：詐欺、洗錢防制法" in message
    assert inserted["params"][5] == "詐欺、洗錢防制法"
