from __future__ import annotations


def test_draft_context_injects_legal_workflow_prompt(monkeypatch):
    from api.osc import drafts

    monkeypatch.setattr(drafts, "_osc_exec", lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(drafts, "_osc_resolve_draft_insights", lambda body: [])
    monkeypatch.setattr(drafts, "_osc_collect_draft_reference_style", lambda body: ("(無參考範本)", [], []))
    monkeypatch.setattr(drafts, "_osc_get_setting_value", lambda key, default="": "")
    monkeypatch.setattr(
        drafts,
        "_get_draft_prompt_template",
        lambda: (
            "文件類型：{doc_type}\n"
            "案由：{reason}\n"
            "事實：{case_facts}\n"
            "學習：{learning_guidance}\n"
        ),
    )
    monkeypatch.setattr(drafts, "learning_guidance_for_prompt", lambda **kwargs: "尚無人工修正紀錄。")

    ctx = drafts._osc_build_draft_context(
        {
            "doc_type": "民事準備書狀",
            "reason": "損害賠償",
            "case_facts": "被告侵權行為造成損害。",
        }
    )

    assert "法律工作流與覆核規則" in ctx["prompt"]
    assert "書狀覆核代理" in ctx["prompt"]
    assert ctx["legal_workflow"]["agent"]["key"] == "pleading_review_agent"
    assert "legal_workflow_source_review_required" in ctx["warnings"]
