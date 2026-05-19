from __future__ import annotations

from api.legal_workflow import (
    append_workflow_footer,
    build_legal_reviewer_note,
    detect_legal_workflow,
    source_tag_for_provenance,
    workflow_prompt_block,
    workflow_review,
)


def test_detect_legal_research_workflow_requires_legal_sources():
    workflow = detect_legal_workflow(text="查法條 民法第184條", mode="answer")

    assert workflow["enabled"] is True
    assert workflow["agent"]["key"] == "legal_research_agent"
    assert "taiwan_legal_mcp_if_available" in workflow["must_use_tools"]


def test_detect_draft_workflow_is_same_reason_guarded():
    workflow = detect_legal_workflow(text="請求損害賠償", doc_type="民事準備書狀", mode="draft")
    prompt = workflow_prompt_block(workflow)

    assert workflow["agent"]["key"] == "pleading_review_agent"
    assert "同案由才套用學習" in prompt
    assert "source_quality_check" in prompt
    assert "來源標記" in prompt
    assert "覆核註記" in prompt


def test_workflow_review_blocks_citations_without_sources():
    workflow = detect_legal_workflow(text="最高法院114年度台上字第123號", mode="draft")
    result = workflow_review("依最高法院114年度台上字第123號意旨。", workflow)

    assert result["pass"] is False
    assert {issue["code"] for issue in result["issues"]} >= {"legal_citation_without_source"}


def test_append_workflow_footer_marks_tool_backed_answer():
    workflow = detect_legal_workflow(text="實務見解 侵權行為", mode="answer")
    text = append_workflow_footer("查詢結果", workflow, tool_used=True)

    assert "法律工作流：實務見解檢索代理" in text
    assert "已啟用可用法律資料來源" in text
    assert "覆核註記" in text


def test_workflow_review_blocks_authority_claim_without_source():
    workflow = detect_legal_workflow(text="最高法院見解", mode="answer")
    result = workflow_review("最高法院認為此類案件應採嚴格審查。", workflow)

    assert result["pass"] is False
    assert {issue["code"] for issue in result["issues"]} >= {"authority_claim_without_source"}


def test_source_tags_and_reviewer_note_are_public_safe():
    assert source_tag_for_provenance("mcp") == "[法律 MCP]"
    assert source_tag_for_provenance("unknown") == "[模型記憶，需核對]"

    note = build_legal_reviewer_note(source_count=2, read_scope="上傳文件全文", currency_checked=True)
    assert "2 個來源" in note
    assert "上傳文件全文" in note
