"""Three Sages (三哲人) verification workflow.

Implements a draft → verify → revise pipeline using three LLM personas:

1. **MELCHIOR** (草稿者) — generates the initial answer draft
2. **BALTHASAR** (查證者) — fact-checks the draft against evidence
3. **CASPER** (批判者) — finds hallucinations, logic gaps, and contradictions

All three share the same local model (Gemma 4 26B on port 8080) and execute
serially to avoid OOM.  At most 1 revision is allowed before the system
force-accepts or abstains.

Trigger conditions (not all queries go through this):
- task_type == "legal_analysis"
- task_type == "summary" and prompt > 3000 chars
- user explicitly requests /深度思考
- general query containing legal keywords
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from api.verification.answer_verifier import AnswerVerificationResult, verify_answer

logger = logging.getLogger(__name__)

VerifierGenerate = Callable[[str], str]
ProgressFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENABLED = os.environ.get("MAGI_TRI_AGENT_ENABLED", "1").strip() in {"1", "true", "yes"}
_ALLOW_LLM_FALLBACK = os.environ.get("MAGI_TRI_AGENT_LLM_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
_MAX_REVISIONS = 1
_TIMEOUT_PER_AGENT = int(os.environ.get("MAGI_TRI_AGENT_TIMEOUT", "45"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass()
class TriAgentVerificationReport:
    passed: bool
    draft_answer: str
    evidence_summary: str
    critic_reason: str
    final_answer: str
    revision_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

MELCHIOR_SYSTEM = """你是 MELCHIOR，負責產出答案草稿。
- 優先引用記憶上下文中的事實
- 遇到不確定的法律問題，標注「待查證」
- 不要自信地編造法條或判例
- 用繁體中文回答
- 不要使用 Markdown 語法"""

BALTHASAR_SYSTEM = """你是 BALTHASAR，負責查證答案。
- 逐句審查草稿，標出「有依據」或「無依據」
- 依據來源：記憶上下文、工具查詢結果、明確法條引用
- 不要補充答案，只做查證
- 回傳 JSON 格式：{"verified": [...], "unverified": [...], "conflicts": [...]}
- 若全部有依據，conflicts 為空陣列"""

CASPER_CRITIC_SYSTEM = """你是 CASPER，負責批判審查。
- 找出幻覺：沒有依據卻斷言的句子
- 找出跳步：邏輯推論缺少中間步驟
- 找出矛盾：草稿內部或與證據矛盾之處
- 判定：PASS（可發布）/ REVISE（需修訂）/ REJECT（需重答）
- 若 REVISE，列出具體修改要求
- 回傳 JSON 格式：{"verdict": "PASS|REVISE|REJECT", "issues": [...], "revision_instructions": "..."}"""


# ---------------------------------------------------------------------------
# Trigger conditions
# ---------------------------------------------------------------------------

_LEGAL_KEYWORDS = re.compile(
    r"(法條|法律|判決|構成要件|損害賠償|侵權|違約|契約|民法|刑法|"
    r"勞基法|行政法|訴訟|裁判|上訴|抗告|聲請|強制執行|假扣押|"
    r"假處分|保全|調解|仲裁|鑑定|律師|法官|檢察官|起訴|公訴|"
    r"自訴|告訴|被告|原告|當事人|證人|鑑定人|辯護人)",
)


def should_trigger_tri_agent(
    *,
    task_type: str = "",
    message: str = "",
    prompt_length: int = 0,
    explicit_deep_think: bool = False,
) -> bool:
    """Determine if the three sages verification should be triggered."""
    if not _ENABLED:
        return False
    if explicit_deep_think:
        return True
    if task_type == "legal_analysis":
        return True
    if task_type == "summary" and prompt_length > 3000:
        return True
    if task_type in {"general", ""} and _LEGAL_KEYWORDS.search(message):
        return True
    return False


# ---------------------------------------------------------------------------
# Evidence summarization
# ---------------------------------------------------------------------------

def _summarize_evidence(*, memories: list[dict] | None, memory_context: str, web_context: str) -> str:
    lines: list[str] = []
    if memories:
        lines.append(f"記憶線索 {len(memories)} 筆")
    if str(memory_context or "").strip() and str(memory_context).strip() != "無相關記憶。":
        lines.append("含可用記憶上下文")
    if str(web_context or "").strip() and str(web_context).strip() not in {"無", "無。"}:
        lines.append("含網路或外部證據")
    return "；".join(lines) if lines else "目前沒有額外證據支撐"


# ---------------------------------------------------------------------------
# LLM call helper
# ---------------------------------------------------------------------------

def _call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    generate: Optional[VerifierGenerate] = None,
    timeout: int = 45,
) -> str:
    """Call the LLM with a system+user prompt pair.

    Uses the provided generate callable, or falls back to inference_gateway.
    """
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    if callable(generate):
        return str(generate(full_prompt) or "").strip()

    # Default to heuristic-only mode unless live fallback is explicitly enabled.
    # This keeps verification deterministic in test/offline environments and
    # avoids blocking on unavailable local inference services.
    if not _ALLOW_LLM_FALLBACK:
        return ""

    try:
        from skills.bridge.inference_gateway import InferenceGateway

        gw = InferenceGateway()
        result = gw.chat(full_prompt, task_type="general", timeout=timeout)
        if isinstance(result, dict):
            return str(result.get("response") or result.get("text") or "").strip()
        return str(result or "").strip()
    except Exception as e:
        logger.warning("Tri-agent LLM call failed: %s", e)
        return ""


def _parse_json_response(text: str) -> dict:
    """Try to extract JSON from LLM response."""
    text = text.strip()
    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Try extracting JSON block
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run_tri_agent_verification(
    *,
    query: str,
    draft_answer: str,
    memories: list[dict] | None = None,
    memory_context: str = "",
    web_context: str = "",
    conversation_history: str = "",
    generate: Optional[VerifierGenerate] = None,
    progress_fn: Optional[ProgressFn] = None,
    timeout: int = 120,
) -> TriAgentVerificationReport:
    """Run the three sages verification pipeline.

    1. MELCHIOR drafts (or uses provided draft_answer)
    2. BALTHASAR fact-checks against evidence
    3. CASPER critiques and decides PASS/REVISE/REJECT
    4. If REVISE → MELCHIOR rewrites once → CASPER re-checks
    """
    t0 = time.monotonic()
    per_agent_timeout = min(_TIMEOUT_PER_AGENT, timeout // 3)
    evidence_summary = _summarize_evidence(
        memories=memories,
        memory_context=memory_context,
        web_context=web_context,
    )

    # ── Stage 1: MELCHIOR — Draft ──
    _notify(progress_fn, "MELCHIOR 草擬中...")
    if not draft_answer.strip():
        melchior_prompt = (
            f"[問題]\n{query}\n\n"
            f"[記憶上下文]\n{memory_context or '無'}\n\n"
            f"[網路研究]\n{web_context or '無'}\n\n"
            f"[近期對話]\n{conversation_history or '無'}"
        )
        draft_answer = _call_llm(
            MELCHIOR_SYSTEM, melchior_prompt,
            generate=generate, timeout=per_agent_timeout,
        )
    if not draft_answer.strip():
        return _make_report(
            passed=False,
            draft_answer="",
            evidence_summary=evidence_summary,
            critic_reason="MELCHIOR 未能產出草稿",
            final_answer="抱歉，我目前無法產出回答。",
        )

    # ── Stage 2: BALTHASAR — Fact-check ──
    _notify(progress_fn, "BALTHASAR 查證中...")
    balthasar_prompt = (
        f"[草稿]\n{draft_answer}\n\n"
        f"[可用證據]\n{memory_context or '無'}\n\n"
        f"[網路研究]\n{web_context or '無'}\n\n"
        "請逐句查證草稿，標出有依據/無依據。"
    )
    balthasar_response = _call_llm(
        BALTHASAR_SYSTEM, balthasar_prompt,
        generate=generate, timeout=per_agent_timeout,
    )
    evidence_map = _parse_json_response(balthasar_response)
    unverified = evidence_map.get("unverified", [])
    conflicts = evidence_map.get("conflicts", [])

    # ── Stage 3: CASPER — Critique ──
    _notify(progress_fn, "CASPER 審查中...")
    casper_prompt = (
        f"[問題]\n{query}\n\n"
        f"[草稿]\n{draft_answer}\n\n"
        f"[查證結果]\n{balthasar_response}\n\n"
        f"[證據摘要]\n{evidence_summary}\n\n"
        "請判定：PASS / REVISE / REJECT"
    )
    casper_response = _call_llm(
        CASPER_CRITIC_SYSTEM, casper_prompt,
        generate=generate, timeout=per_agent_timeout,
    )
    critic_result = _parse_json_response(casper_response)
    verdict = str(critic_result.get("verdict", "")).upper()
    critic_issues = critic_result.get("issues", [])
    revision_instructions = str(critic_result.get("revision_instructions", ""))

    # Also run heuristic verifier
    heuristic = verify_answer(
        query=query,
        answer=draft_answer,
        memories=memories,
        memory_context=memory_context,
        web_context=web_context,
        conversation_history=conversation_history,
    )

    # If CASPER LLM call failed (empty response), fall back to heuristic-only
    llm_available = bool(casper_response.strip())
    if not llm_available:
        verdict = "PASS" if heuristic.passed else "REVISE"

    # Combine: LLM verdict + heuristic
    if verdict == "PASS" and heuristic.passed:
        return _make_report(
            passed=True,
            draft_answer=draft_answer,
            evidence_summary=evidence_summary,
            critic_reason="三哲人驗證通過",
            final_answer=draft_answer,
            metadata={
                "unverified_count": len(unverified),
                "conflict_count": len(conflicts),
                "heuristic": heuristic.metadata,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            },
        )

    # ── Stage 4: MELCHIOR Revision (max 1) ──
    if verdict in {"REVISE", ""} or not heuristic.passed:
        _notify(progress_fn, "MELCHIOR 修訂中...")
        revision_prompt = (
            f"[問題]\n{query}\n\n"
            f"[原始草稿]\n{draft_answer}\n\n"
            f"[批判意見]\n{casper_response}\n\n"
            f"[具體修改要求]\n{revision_instructions or heuristic.reason}\n\n"
            f"[證據摘要]\n{evidence_summary}\n\n"
            "規則：\n"
            "- 不要聲稱有不存在的既有記憶\n"
            "- 沒證據就明說目前無法確認\n"
            "- 使用繁體中文\n"
            "- 直接輸出修正版"
        )
        revised = _call_llm(
            MELCHIOR_SYSTEM, revision_prompt,
            generate=generate, timeout=per_agent_timeout,
        )
        if revised.strip():
            revised_check = verify_answer(
                query=query,
                answer=revised,
                memories=memories,
                memory_context=memory_context,
                web_context=web_context,
                conversation_history=conversation_history,
            )
            return _make_report(
                passed=revised_check.passed,
                draft_answer=draft_answer,
                evidence_summary=evidence_summary,
                critic_reason=f"修訂後{'通過' if revised_check.passed else '仍有疑慮'}",
                final_answer=revised if revised_check.passed else (revised_check.safe_reply or revised),
                revision_count=1,
                metadata={
                    "original_verdict": verdict,
                    "heuristic_issues": heuristic.issues,
                    "critic_issues": critic_issues,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
            )

    # REJECT or all fallbacks exhausted
    safe = heuristic.safe_reply or "我目前無法確認這個問題的答案，建議直接查閱原始資料。"
    return _make_report(
        passed=False,
        draft_answer=draft_answer,
        evidence_summary=evidence_summary,
        critic_reason=f"verdict={verdict}, issues={critic_issues}",
        final_answer=safe,
        metadata={
            "original_verdict": verdict,
            "heuristic_issues": heuristic.issues,
            "critic_issues": critic_issues,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notify(fn: Optional[ProgressFn], msg: str) -> None:
    if callable(fn):
        try:
            fn(msg)
        except Exception:
            pass


def _make_report(
    *,
    passed: bool,
    draft_answer: str,
    evidence_summary: str,
    critic_reason: str,
    final_answer: str,
    revision_count: int = 0,
    metadata: dict[str, Any] | None = None,
) -> TriAgentVerificationReport:
    return TriAgentVerificationReport(
        passed=passed,
        draft_answer=draft_answer,
        evidence_summary=evidence_summary,
        critic_reason=critic_reason,
        final_answer=final_answer,
        revision_count=revision_count,
        metadata=metadata or {},
    )


def format_verification_footer(report: TriAgentVerificationReport) -> str:
    """Format a short verification summary footer for the user."""
    if report.passed and report.revision_count == 0:
        return "三哲人驗證通過"
    if report.passed and report.revision_count > 0:
        return f"三哲人驗證（修訂 {report.revision_count} 次）通過"
    return "三哲人驗證未通過，已改為保守回答"
