from __future__ import annotations

from api.legal_workflow import (
    append_workflow_footer,
    detect_legal_workflow,
    workflow_prompt_block,
    workflow_review,
)


def test_detect_legal_research_workflow_is_public_safe():
    workflow = detect_legal_workflow(text="查法條 民法第184條", mode="answer")

    assert workflow["enabled"] is True
    assert workflow["agent"]["key"] == "legal_research_agent"
    assert workflow["must_use_tools"] == ["configured_legal_sources"]


def test_draft_workflow_keeps_same_reason_guardrail():
    workflow = detect_legal_workflow(text="請求損害賠償", doc_type="民事準備書狀", mode="draft")
    prompt = workflow_prompt_block(workflow)

    assert workflow["agent"]["key"] == "pleading_review_agent"
    assert "同案由才套用學習" in prompt
    assert "source_quality_check" in prompt


def test_workflow_review_blocks_citation_without_source():
    workflow = detect_legal_workflow(text="最高法院114年度台上字第123號", mode="legal")
    result = workflow_review("依最高法院114年度台上字第123號意旨。", workflow)

    assert result["pass"] is False
    assert "legal_citation_without_source" in {issue["code"] for issue in result["issues"]}


def test_public_placeholder_returns_workflow_guidance():
    from api.domains import judgment_flow

    text = judgment_flow.run_judgment_collector_command(None, "查判決 遲延交屋")

    assert "does not include legal-research collection" in text
    assert "法律工作流：實務見解檢索代理" in text
    assert "Configured" not in text
