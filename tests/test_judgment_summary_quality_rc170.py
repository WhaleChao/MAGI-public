from __future__ import annotations

from api.domains.judgment_summary_quality import (
    build_extractive_practice_summary,
    evaluate_practice_ready_summary,
    evaluate_practice_summary,
    screen_stored_summary,
    select_practice_spans,
)
from api.domains import judgment_flow


SUBSTANTIVE_JUDGMENT = """\
臺灣臺北地方法院民事判決
主文
原告之訴駁回。
事實及理由
按民法第184條規定，侵權行為之成立，應以行為人有故意或過失、權利受侵害及二者間具有相當因果關係為要件。
原告主張被告應賠償醫療費及精神慰撫金，並提出診斷證明為證。
本院認為原告就損害與被告行為間之因果關係，依民事訴訟法第277條規定應負舉證責任，原告未提出足以證明之資料，故其請求不得准許。
中華民國115年7月30日
"""

GENERIC_APPEAL = """\
最高法院民事裁定
主文
上訴駁回。
理由
按上訴第三審法院，非以原判決違背法令為理由，不得為之；又上訴狀內應記載上訴理由，表明原判決所違背之法令及其具體內容。
上訴人僅泛言原判決不當，未具體表明上訴理由，其上訴不合法。
中華民國115年7月30日
"""


def test_source_bound_summary_contains_issue_rule_application_and_outcome() -> None:
    summary = build_extractive_practice_summary(
        SUBSTANTIVE_JUDGMENT,
        "侵權行為損害賠償",
    )
    assert "## 法律爭點" in summary
    assert "## 實務見解" in summary
    assert "因果關係為要件" in summary
    assert "## 法院涵攝" in summary
    assert "民事訴訟法第277條" in summary
    assert "## 裁判結果" in summary
    assert "原告之訴駁回" in summary
    assert "原文擷取；未以模型改寫" in summary
    quality = evaluate_practice_summary(
        summary,
        SUBSTANTIVE_JUDGMENT,
        "侵權行為損害賠償",
    )
    assert quality.ok
    assert quality.source_supported_spans >= 1
    assert quality.score >= 70
    assert evaluate_practice_ready_summary(
        summary,
        SUBSTANTIVE_JUDGMENT,
        "侵權行為損害賠償",
        "臺灣臺北地方法院",
    ).ok


def test_practice_ready_gate_rejects_trial_rule_without_case_application() -> None:
    source = """臺灣臺北地方法院民事判決
理由
按民法第184條規定，侵權行為損害賠償請求權之成立，應以行為人有故意或過失、權利受侵害及相當因果關係為要件，並由請求權人負舉證責任。
"""
    summary = """## 法律爭點
- 侵權行為損害賠償
## 實務見解
- 按民法第184條規定，侵權行為損害賠償請求權之成立，應以行為人有故意或過失、權利受侵害及相當因果關係為要件，並由請求權人負舉證責任。
"""
    stored = evaluate_practice_summary(summary, source, "侵權行為損害賠償")
    assert stored.ok
    ready = evaluate_practice_ready_summary(
        summary,
        source,
        "侵權行為損害賠償",
        "臺灣臺北地方法院",
        min_score=70,
    )
    assert not ready.ok
    assert ready.reason == "missing_case_application"


def test_fact_only_material_is_not_promoted_to_practical_insight() -> None:
    fact_only = """\
臺灣臺北地方法院刑事判決
主文
被告有罪。
理由
經查被告於民國一百十五年一月一日到場，證人亦到場陳述，並提出照片三張。
中華民國115年7月30日
"""
    rules, applications = select_practice_spans(fact_only, "傷害")
    assert rules == []
    assert applications == []
    assert build_extractive_practice_summary(fact_only, "傷害") == ""


def test_generic_third_instance_boilerplate_does_not_masquerade_as_damages_rule() -> None:
    assert build_extractive_practice_summary(GENERIC_APPEAL, "損害賠償") == ""
    old_summary = """\
## 實務見解
- 按上訴第三審法院，非以原判決違背法令為理由，不得為之；又上訴狀內應記載上訴理由，表明原判決所違背之法令及其具體內容。
## 適用法條
民事訴訟法第467條
"""
    quality = screen_stored_summary(old_summary, "損害賠償")
    assert not quality.ok
    assert quality.reason in {
        "generic_procedure_only",
        "case_issue_mismatch",
        "missing_substantive_rule",
    }


def test_procedural_rule_remains_usable_when_procedure_is_the_actual_issue() -> None:
    summary = build_extractive_practice_summary(GENERIC_APPEAL, "第三審上訴合法性")
    assert "## 實務見解" in summary
    assert "上訴第三審法院" in summary
    assert evaluate_practice_summary(
        summary,
        GENERIC_APPEAL,
        "第三審上訴合法性",
    ).ok


def test_template_and_prompt_echo_are_rejected_even_when_long() -> None:
    template = """\
## 實務見解
（一句話概括本判決的核心法律見解）本院認為……（後接法律原則論述）。
## 適用法條
（列出本判決適用的法條）
"""
    prompt = """\
你是一位精確的法律助理。
## 實務見解
- 按民法第184條規定，侵權行為應具備因果關係。
【嚴格規則】請逐字擷取。
"""
    assert screen_stored_summary(template, "侵權行為").reason == "template_or_placeholder"
    assert screen_stored_summary(prompt, "侵權行為").reason == "prompt_or_trace_leak"


def test_old_fast_digest_is_not_promoted_even_if_one_rule_looks_useful() -> None:
    fast_digest = """\
## 摘要型別
抽取式快篩（主文與理由均取自裁判原文；未經 LLM 改寫）

## 實務見解
- 又個人資料保護法第20條第1項規定，非公務機關利用個人資料，原則上應在蒐集之特定目的必要範圍內為之。
"""
    quality = screen_stored_summary(fast_digest, "個人資料保護法")
    assert not quality.ok
    assert quality.reason == "fast_digest_preview"


def test_source_support_gate_rejects_fluent_hallucinated_rule() -> None:
    summary = """\
## 法律爭點
- 侵權行為損害賠償
## 實務見解
- 按民法第184條規定，精神慰撫金一律不得超過新臺幣十萬元。
## 適用法條
民法第184條
"""
    quality = evaluate_practice_summary(
        summary,
        SUBSTANTIVE_JUDGMENT,
        "侵權行為損害賠償",
    )
    assert not quality.ok
    assert quality.reason == "unsupported_opinion"


def test_shared_quality_gate_rejects_too_short_legal_fragment() -> None:
    source = "理由\n按民法第184條規定，侵權行為人應負損害賠償責任。"
    summary = """\
## 法律爭點
- 損害賠償
## 實務見解
- 按民法第184條規定，侵權行為人應負損害賠償責任。
"""
    quality = evaluate_practice_summary(summary, source, "損害賠償")
    assert not quality.ok
    assert quality.reason == "opinion_too_short"


def test_research_flow_rejects_structured_but_irrelevant_legacy_summary() -> None:
    item = {
        "title": "最高法院115年度台上字第1號",
        "case_type": "損害賠償",
        "summary_preview": (
            "## 實務見解\n"
            "- 按上訴第三審法院，非以原判決違背法令為理由，不得為之；"
            "又上訴狀內應記載上訴理由，表明原判決所違背之法令及其具體內容。\n"
            "## 適用法條\n民事訴訟法第467條"
        ),
    }
    assert judgment_flow._judgment_item_quality_issue(item) == "low_practical_value"


def test_summary_never_truncates_an_exact_source_quote() -> None:
    summary = build_extractive_practice_summary(
        SUBSTANTIVE_JUDGMENT,
        "侵權行為損害賠償",
        max_chars=120,
    )
    assert summary == ""
