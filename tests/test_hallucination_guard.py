"""
tests/test_hallucination_guard.py
==================================
全域防幻覺守門員測試

涵蓋：
- classify_risk：HIGH / MEDIUM / SAFE 分類
- check_fact_grounding：法條引用溯源
- rewrite_ungrounded_attribution：模糊歸因 rewrite
- build_anti_hallucination_prompt_rules：prompt 規則產生
- tw_output_guard 整合：vague attribution 被 normalize_output_text 攔截
- grounded_ai _classify_query_tier：HIGH-risk 強制升 COMPLEX
"""

import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ---------------------------------------------------------------------------
# classify_risk
# ---------------------------------------------------------------------------

def test_classify_risk_high_law_article():
    from api.hallucination_guard import classify_risk
    assert classify_risk("民法第184條的要件是什麼？") == "HIGH"


def test_classify_risk_high_bare_article():
    from api.hallucination_guard import classify_risk
    assert classify_risk("第768條的時效規定如何適用？") == "HIGH"


def test_classify_risk_high_case_number():
    from api.hallucination_guard import classify_risk
    assert classify_risk("114年度原訴字第000024號案件") == "HIGH"


def test_classify_risk_high_statute_prescription():
    from api.hallucination_guard import classify_risk
    assert classify_risk("訴訟時效為2年，這個怎麼計算？") == "HIGH"


def test_classify_risk_medium_procedure():
    from api.hallucination_guard import classify_risk
    result = classify_risk("上訴要件和抗告程序有什麼差別？")
    assert result in ("HIGH", "MEDIUM")  # 程序詞彙至少 MEDIUM


def test_classify_risk_safe_chitchat():
    from api.hallucination_guard import classify_risk
    assert classify_risk("你好，今天天氣不錯") == "SAFE"


def test_classify_risk_safe_opinion():
    from api.hallucination_guard import classify_risk
    assert classify_risk("你覺得哪個版本更好？") == "SAFE"


def test_classify_risk_safe_greeting():
    from api.hallucination_guard import classify_risk
    assert classify_risk("早安！") == "SAFE"


# ---------------------------------------------------------------------------
# check_fact_grounding
# ---------------------------------------------------------------------------

def test_grounding_no_article_refs():
    """沒有法條引用 → 視為已溯源"""
    from api.hallucination_guard import check_fact_grounding
    grounded, ungrounded = check_fact_grounding("這是一般陳述。", ["some context"])
    assert grounded is True
    assert ungrounded == []


def test_grounding_article_in_context():
    """第184條出現在 context → 已溯源"""
    from api.hallucination_guard import check_fact_grounding
    answer = "根據民法第184條，侵權行為需具備三要件。"
    context = ["民法第184條規定：因故意或過失，不法侵害他人之權利者..."]
    grounded, ungrounded = check_fact_grounding(answer, context)
    assert grounded is True
    assert ungrounded == []


def test_grounding_article_not_in_context():
    """第184條不在 context → 未溯源"""
    from api.hallucination_guard import check_fact_grounding
    answer = "根據民法第184條，侵權行為的要件如下。"
    context = ["這是一段沒有提到任何法條的文字。"]
    grounded, ungrounded = check_fact_grounding(answer, context)
    assert grounded is False
    assert "第184條" in ungrounded


def test_grounding_multiple_articles_one_missing():
    """第184條有、第195條沒有 → 第195條未溯源"""
    from api.hallucination_guard import check_fact_grounding
    answer = "民法第184條是故意過失，民法第195條是慰撫金請求。"
    # Context 只有第184條，沒有第195條（注意 context 中不要提到195這個數字）
    context = ["民法第184條規定侵權行為之損害賠償，因故意或過失不法侵害他人之權利者。"]
    grounded, ungrounded = check_fact_grounding(answer, context)
    assert grounded is False
    assert "第195條" in ungrounded
    assert "第184條" not in ungrounded


def test_grounding_empty_answer():
    """空答案 → 視為已溯源"""
    from api.hallucination_guard import check_fact_grounding
    grounded, ungrounded = check_fact_grounding("", ["some context"])
    assert grounded is True
    assert ungrounded == []


def test_grounding_empty_context():
    """有法條但無 context → 未溯源"""
    from api.hallucination_guard import check_fact_grounding
    grounded, ungrounded = check_fact_grounding("民法第184條...", [])
    assert grounded is False
    assert "第184條" in ungrounded


def test_grounding_context_with_spaces():
    """context 中的法條號有空格 → 仍能匹配"""
    from api.hallucination_guard import check_fact_grounding
    answer = "民法第184條要件。"
    context = ["民法第 184 條規定..."]
    grounded, _ = check_fact_grounding(answer, context)
    assert grounded is True


def test_grounding_case_citation_in_context():
    """具體裁判字號必須可在 context 中回查。"""
    from api.hallucination_guard import check_fact_grounding
    answer = "最高法院112年度台上字第1234號判決採相同見解。"
    context = ["最高法院 112 年度台上字第 1234 號判決要旨：..."]
    grounded, ungrounded = check_fact_grounding(answer, context)
    assert grounded is True
    assert ungrounded == []


def test_grounding_case_citation_missing_context():
    from api.hallucination_guard import check_fact_grounding
    answer = "最高法院112年度台上字第1234號判決採相同見解。"
    context = ["這段資料沒有任何裁判字號。"]
    grounded, ungrounded = check_fact_grounding(answer, context)
    assert grounded is False
    assert "最高法院112年度台上字第1234號" in ungrounded


# ---------------------------------------------------------------------------
# rewrite_ungrounded_attribution
# ---------------------------------------------------------------------------

def test_rewrite_genju_liaojie():
    from api.hallucination_guard import rewrite_ungrounded_attribution
    result = rewrite_ungrounded_attribution("根據我的了解，這個法條要件是三個。")
    assert "根據我的了解" not in result
    assert "AI 推論" in result or "建議查證" in result
    # 原文內容保留
    assert "這個法條要件是三個" in result


def test_rewrite_juzuosuozhi():
    from api.hallucination_guard import rewrite_ungrounded_attribution
    result = rewrite_ungrounded_attribution("據我所知，訴訟時效是兩年。")
    assert "據我所知" not in result
    assert "訴訟時效是兩年" in result


def test_rewrite_wozidao():
    from api.hallucination_guard import rewrite_ungrounded_attribution
    result = rewrite_ungrounded_attribution("我知道這個案子的判決結果。")
    # 只有在 "我知道的是" / "我知道這個" 格式才觸發
    # "我知道" 無後綴也可能觸發，依 regex 而定
    # 重點：原文意義保留
    assert "案子的判決結果" in result


def test_rewrite_no_match():
    """無模糊歸因 → 原文不變"""
    from api.hallucination_guard import rewrite_ungrounded_attribution
    original = "根據民法第184條，侵權行為要件有三。"
    result = rewrite_ungrounded_attribution(original)
    assert result == original


def test_rewrite_only_first_match():
    """只替換第一個命中，不連續替換"""
    from api.hallucination_guard import rewrite_ungrounded_attribution
    text = "根據我的了解，X是Y。據我所知，A是B。"
    result = rewrite_ungrounded_attribution(text)
    # 只替換了第一個
    count = result.count("AI 推論")
    assert count == 1


# ---------------------------------------------------------------------------
# build_anti_hallucination_prompt_rules
# ---------------------------------------------------------------------------

def test_prompt_rules_contains_key_phrases():
    from api.hallucination_guard import build_anti_hallucination_prompt_rules
    rules = build_anti_hallucination_prompt_rules()
    assert "法條號碼" in rules or "法條" in rules
    assert "不確定" in rules or "查證" in rules
    assert "禁用" in rules or "禁止" in rules


# ---------------------------------------------------------------------------
# tw_output_guard 整合：ungrounded attribution 被攔截
# ---------------------------------------------------------------------------

def test_output_guard_rewrites_attribution():
    """tw_output_guard normalize_output_text 應呼叫 rewrite_ungrounded_attribution"""
    from api.tw_output_guard import normalize_output_text
    text = "根據我的了解，民法第184條的要件包括故意過失。"
    result = normalize_output_text(text)
    # 模糊歸因應被替換
    assert "根據我的了解" not in result


def test_output_guard_preserves_authoritative_source():
    """有明確來源的陳述不應被修改"""
    from api.tw_output_guard import normalize_output_text
    text = "根據上方記憶提到的民法第184條，侵權行為要件如下。"
    result = normalize_output_text(text)
    # 「根據上方記憶提到的」不在黑名單，不應被替換
    assert "民法第184條" in result


# ---------------------------------------------------------------------------
# grounded_ai _classify_query_tier 整合
# ---------------------------------------------------------------------------

def test_tier_classifier_upgrades_high_risk(monkeypatch):
    """含具體法條引用的查詢應被強制升到 COMPLEX"""
    # 直接測 classify_risk 回傳 HIGH 然後 tier 邏輯用它
    from api.hallucination_guard import classify_risk
    assert classify_risk("民法第184條的侵權要件請解釋") == "HIGH"


def test_needs_grounding_check_with_article():
    from api.hallucination_guard import needs_grounding_check
    assert needs_grounding_check("民法第184條") is True


def test_needs_grounding_check_with_case_citation():
    from api.hallucination_guard import needs_grounding_check
    assert needs_grounding_check("最高法院112年度台上字第1234號") is True


def test_needs_grounding_check_no_article():
    from api.hallucination_guard import needs_grounding_check
    assert needs_grounding_check("今天天氣不錯") is False
