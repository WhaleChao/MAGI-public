# -*- coding: utf-8 -*-
from __future__ import annotations

from scripts.ops.smoke_core_routes import Case, _cases, _classify_case_output


def test_classify_pass_with_alternative_phrase():
    case = Case("translate_guide", "你會翻譯嗎？", ("我可以幫您翻譯", "翻譯結果"))
    status = _classify_case_output(case, "🌐 翻譯結果（google_gtx_primary）: 你會翻譯嗎？")
    assert status == "PASS"


def test_classify_warn_for_missing_dependency():
    case = Case("judgment_guide", "你會查判決嗎？", "我可以幫您查判決", warn_substring=("missing API key",))
    status = _classify_case_output(case, "❌ 判決搜尋失敗：unauthorized: missing API key")
    assert status == "WARN"


def test_summary_exec_prompt_contains_explicit_content_marker():
    summary_case = next(case for case in _cases() if case.name == "summary_exec")

    assert "待摘要內容" in summary_case.message
    assert summary_case.message.startswith("@heavy ")


def test_summary_exec_requires_summarized_source_content():
    summary_case = next(case for case in _cases() if case.name == "summary_exec")

    assert _classify_case_output(summary_case, "摘要結果：請提供需要摘要的短文。") == "FAIL"
    assert _classify_case_output(summary_case, "摘要結果：- 第一點很重要。") == "PASS"


def test_guide_cases_reject_accidental_execution_results():
    translate_case = next(case for case in _cases() if case.name == "translate_guide")
    summary_case = next(case for case in _cases() if case.name == "summary_guide")
    judgment_case = next(case for case in _cases() if case.name == "judgment_guide")

    assert _classify_case_output(translate_case, "🌐 翻譯結果（google_gtx_primary）: 你會翻譯嗎？") == "FAIL"
    assert _classify_case_output(summary_case, "📝 摘要結果（fake）: 請提供您需要我分析的內容") == "FAIL"
    assert _classify_case_output(judgment_case, "📚 判決搜尋完成：你會查判決嗎？ 收集筆數：2") == "FAIL"


def test_heavy_exec_cases_include_heavy_prefix():
    cases = {case.name: case for case in _cases()}

    assert cases["translate_exec"].message.startswith("@heavy ")
    assert cases["summary_exec"].message.startswith("@heavy ")
