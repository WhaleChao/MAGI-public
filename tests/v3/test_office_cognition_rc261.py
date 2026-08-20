from __future__ import annotations

import pytest

from api.agentic.contracts import SideEffectLevel
from api.routing.office_cognition import assess_office_request


@pytest.mark.parametrize(
    ("message", "key"),
    [
        ("幫我查一下案件", "case_target"),
        ("幫我報結", "laf_case_target"),
        ("幫我寫一份書狀", "draft_target"),
        ("幫我找判決", "legal_issue"),
        ("幫我翻譯這份文件", "file_target"),
        ("把行程改到3點", "schedule_target"),
        ("把2026-0062的開庭改到3點", "schedule_meridiem"),
    ],
)
def test_human_assistant_stops_on_material_missing_fact(message: str, key: str) -> None:
    result = assess_office_request(message)
    assert result.needs_clarification is True
    assert result.clarification_key == key
    assert result.envelope.complete is False


@pytest.mark.parametrize(
    "message",
    [
        "查詢2026-0062案件進度",
        "下載2026-0062的判決.pdf",
        "找最高法院關於詐欺取財故意的實務見解",
        "替2026-0062草擬答辯狀",
        "把2026-0062的開庭改到下午3點",
    ],
)
def test_specific_office_requests_do_not_over_question(message: str) -> None:
    result = assess_office_request(message)
    assert result.needs_clarification is False
    assert result.envelope.complete is True


def test_attachment_resolves_content_pronoun() -> None:
    result = assess_office_request("幫我摘要這份文件", has_attachment=True)
    assert result.needs_clarification is False
    assert result.primary_domain in {"file", "summary"}


def test_contract_exposes_tool_and_side_effect_without_running_anything() -> None:
    lookup = assess_office_request("查詢2026-0062案件進度")
    write = assess_office_request("替2026-0062草擬答辯狀")
    delete = assess_office_request("刪除2026-0062案件紀錄")

    assert lookup.tool_requirement.level == "required"
    assert lookup.envelope.side_effect is SideEffectLevel.READ
    assert write.envelope.side_effect is SideEffectLevel.WRITE
    assert delete.envelope.side_effect is SideEffectLevel.DESTRUCTIVE


def test_office_domains_are_visible_for_cross_module_work() -> None:
    result = assess_office_request("把2026-0062判決PDF摘要後附到法扶回報")
    names = {candidate.name for candidate in result.candidates}
    assert {"case", "file", "legal_aid", "summary", "legal_research"}.issubset(names)
    assert result.tool_requirement.level == "required"
    assert result.tool_requirement.tool_hint == "document_processing"


def test_multi_domain_office_fact_query_requires_verified_tool() -> None:
    result = assess_office_request("查2026-0062的法扶附件與明天庭期")
    assert result.tool_requirement.level == "required"
    assert result.tool_requirement.tool_hint == "case_query"
