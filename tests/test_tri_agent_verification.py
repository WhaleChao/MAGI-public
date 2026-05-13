"""Tests for the Three Sages (三哲人) verification workflow."""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.verification import (
    run_tri_agent_verification,
    should_trigger_tri_agent,
    format_verification_footer,
)


def test_tri_agent_verification_accepts_grounded_answer():
    """Heuristic-only mode: grounded answer with evidence passes."""
    report = run_tri_agent_verification(
        query="最新狀況如何？",
        draft_answer="目前官方公告已上線。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="來源 A：官方公告已上線",
        conversation_history="",
    )
    assert report.passed is True
    assert report.final_answer == "目前官方公告已上線。"


def test_tri_agent_verification_handles_false_memory_claim():
    """False memory claim without evidence → either rejected or revised to remove the claim."""
    report = run_tri_agent_verification(
        query="那篇文章是什麼？",
        draft_answer="你之前給過我一篇文章，我現在可以直接幫你整理。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="無。",
        conversation_history="",
    )
    # Either the system rejected it OR successfully revised away the false claim
    if report.passed:
        # If it passed after revision, the false memory claim should be gone
        assert report.revision_count >= 1
        assert "你之前給過我" not in report.final_answer
    else:
        # If rejected, the safe reply should not contain the false claim
        assert "你之前給過我" not in report.final_answer


def test_tri_agent_verification_uses_repair_generator_when_available():
    """With a generate callable, revision produces corrected answer."""
    report = run_tri_agent_verification(
        query="那篇文章是什麼？",
        draft_answer="你之前給過我一篇文章，我現在可以直接幫你整理。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="無。",
        conversation_history="",
        generate=lambda _prompt: "我目前沒有可驗證證據能證明你之前提供過那份文章。",
    )
    assert report.revision_count >= 1
    assert "可驗證證據" in report.final_answer or "無法確認" in report.final_answer


def test_tri_agent_with_full_generate():
    """When generate is provided, all three stages use it."""
    call_log: list[str] = []

    def mock_generate(prompt: str) -> str:
        call_log.append(prompt[:50])
        if "BALTHASAR" in prompt:
            return '{"verified": ["所有句子"], "unverified": [], "conflicts": []}'
        if "CASPER" in prompt:
            return '{"verdict": "PASS", "issues": [], "revision_instructions": ""}'
        return "測試回答"

    report = run_tri_agent_verification(
        query="民法第184條的構成要件？",
        draft_answer="侵權行為需要故意或過失。",
        memories=[],
        memory_context="記憶：侵權行為構成要件包括故意過失、違法性、因果關係、損害",
        web_context="無。",
        conversation_history="",
        generate=mock_generate,
    )
    assert report.passed is True
    assert report.final_answer == "侵權行為需要故意或過失。"
    # BALTHASAR and CASPER were called
    assert len(call_log) >= 2


def test_tri_agent_casper_rejects_and_melchior_revises():
    """CASPER rejects → MELCHIOR revises → should pass if revision is clean."""
    revision_count = [0]

    def mock_generate(prompt: str) -> str:
        if "BALTHASAR" in prompt:
            return '{"verified": ["句子1"], "unverified": ["有問題的句子"], "conflicts": []}'
        if "CASPER" in prompt:
            return '{"verdict": "REVISE", "issues": ["無依據斷言"], "revision_instructions": "移除無依據句子"}'
        if "修正" in prompt or "修改" in prompt or "修訂" in prompt or "MELCHIOR" in prompt:
            revision_count[0] += 1
            return "根據目前可確認的資料，侵權行為需要故意或過失。"
        return "原始草稿"

    report = run_tri_agent_verification(
        query="侵權行為的要件？",
        draft_answer="毫無疑問，侵權行為一定是故意的。",
        memories=[],
        memory_context="記憶：侵權行為包括故意與過失",
        web_context="無。",
        conversation_history="",
        generate=mock_generate,
    )
    assert report.revision_count >= 1


# ── Trigger conditions ──


def test_trigger_legal_analysis():
    assert should_trigger_tri_agent(task_type="legal_analysis")


def test_trigger_long_summary():
    assert should_trigger_tri_agent(task_type="summary", prompt_length=4000)


def test_no_trigger_short_summary():
    assert not should_trigger_tri_agent(task_type="summary", prompt_length=2000)


def test_trigger_deep_think():
    assert should_trigger_tri_agent(explicit_deep_think=True)


def test_trigger_legal_keywords():
    assert should_trigger_tri_agent(message="請問民法第184條的構成要件是什麼")
    assert should_trigger_tri_agent(message="這個判決的法律見解是什麼")


def test_no_trigger_general_chat():
    assert not should_trigger_tri_agent(message="今天天氣真好")
    assert not should_trigger_tri_agent(message="你好嗎")


def test_no_trigger_translation():
    assert not should_trigger_tri_agent(task_type="translate", message="翻譯這份文件")


def test_trigger_disabled_by_env(monkeypatch):
    monkeypatch.setattr(
        "api.verification.agent_workflow._ENABLED", False,
    )
    assert not should_trigger_tri_agent(task_type="legal_analysis")


# ── Footer formatting ──


def test_footer_passed():
    from api.verification.agent_workflow import TriAgentVerificationReport
    report = TriAgentVerificationReport(
        passed=True, draft_answer="", evidence_summary="",
        critic_reason="", final_answer="", revision_count=0,
    )
    footer = format_verification_footer(report)
    assert "通過" in footer
    assert "修訂" not in footer


def test_footer_passed_with_revision():
    from api.verification.agent_workflow import TriAgentVerificationReport
    report = TriAgentVerificationReport(
        passed=True, draft_answer="", evidence_summary="",
        critic_reason="", final_answer="", revision_count=1,
    )
    footer = format_verification_footer(report)
    assert "修訂 1 次" in footer
    assert "通過" in footer


def test_footer_failed():
    from api.verification.agent_workflow import TriAgentVerificationReport
    report = TriAgentVerificationReport(
        passed=False, draft_answer="", evidence_summary="",
        critic_reason="", final_answer="", revision_count=0,
    )
    footer = format_verification_footer(report)
    assert "未通過" in footer
