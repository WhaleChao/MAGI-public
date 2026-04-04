from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from api.agents import AgentCoordinator, AgentRuntime, AgentSpec
from api.verification.answer_verifier import AnswerVerificationResult, verify_answer


VerifierGenerate = Callable[[str], str]


@dataclass(slots=True)
class TriAgentVerificationReport:
    passed: bool
    draft_answer: str
    evidence_summary: str
    critic_reason: str
    final_answer: str
    revision_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _summarize_evidence(*, memories: list[dict] | None, memory_context: str, web_context: str) -> str:
    lines: list[str] = []
    if memories:
        lines.append(f"記憶線索 {len(memories)} 筆")
    if str(memory_context or "").strip() and str(memory_context).strip() != "無相關記憶。":
        lines.append("含可用記憶上下文")
    if str(web_context or "").strip() and str(web_context).strip() not in {"無", "無。"}:
        lines.append("含網路或外部證據")
    return "；".join(lines) if lines else "目前沒有額外證據支撐"


def _build_coordinator() -> AgentCoordinator:
    coordinator = AgentCoordinator(name="magi-tri-verify")
    passthrough = lambda message, **_context: message
    coordinator.register_agent(AgentRuntime(spec=AgentSpec(name="draft", role="draft"), responder=passthrough))
    coordinator.register_agent(AgentRuntime(spec=AgentSpec(name="evidence", role="evidence"), responder=passthrough))
    coordinator.register_agent(AgentRuntime(spec=AgentSpec(name="critic", role="critic"), responder=passthrough))
    return coordinator


def run_tri_agent_verification(
    *,
    query: str,
    draft_answer: str,
    memories: list[dict] | None = None,
    memory_context: str = "",
    web_context: str = "",
    conversation_history: str = "",
    generate: VerifierGenerate | None = None,
) -> TriAgentVerificationReport:
    coordinator = _build_coordinator()
    draft_response = coordinator.dispatch(
        draft_answer,
        agent="draft",
    )
    evidence_summary = _summarize_evidence(
        memories=memories,
        memory_context=memory_context,
        web_context=web_context,
    )
    coordinator.dispatch(evidence_summary, agent="evidence")

    initial = verify_answer(
        query=query,
        answer=draft_response.content,
        memories=memories,
        memory_context=memory_context,
        web_context=web_context,
        conversation_history=conversation_history,
    )
    coordinator.dispatch(initial.reason or "verified", agent="critic")
    if initial.passed:
        return TriAgentVerificationReport(
            passed=True,
            draft_answer=draft_response.content,
            evidence_summary=evidence_summary,
            critic_reason=initial.reason,
            final_answer=draft_response.content,
            metadata={"verification": initial.metadata, "agent_count": 3},
        )

    final_answer = initial.safe_reply
    revision_count = 0
    if callable(generate):
        repair_prompt = (
            "你是 MAGI 的最終校稿代理。請根據以下資訊修正回答。\n"
            f"[問題]\n{query}\n\n"
            f"[草稿]\n{draft_response.content}\n\n"
            f"[證據摘要]\n{evidence_summary}\n\n"
            f"[批判原因]\n{initial.reason}\n\n"
            "規則：\n"
            "- 不要聲稱有不存在的既有記憶。\n"
            "- 沒證據就明說目前無法確認。\n"
            "- 使用繁體中文。\n"
            "- 直接輸出修正版。\n"
        )
        repaired = str(generate(repair_prompt) or "").strip()
        if repaired:
            revision_count = 1
            repaired_check = verify_answer(
                query=query,
                answer=repaired,
                memories=memories,
                memory_context=memory_context,
                web_context=web_context,
                conversation_history=conversation_history,
            )
            if repaired_check.passed:
                final_answer = repaired
                initial = repaired_check

    return TriAgentVerificationReport(
        passed=initial.passed,
        draft_answer=draft_response.content,
        evidence_summary=evidence_summary,
        critic_reason=initial.reason,
        final_answer=final_answer,
        revision_count=revision_count,
        metadata={"verification": initial.metadata, "agent_count": 3},
    )
