from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from api.session.provenance import parse_source_provenance


_FALSE_MEMORY_PATTERNS = [
    r"你之前(?:有|曾)?(?:給過|提供過|貼過|傳過)我",
    r"你之前(?:有|曾)?(?:說過|提過)",
    r"根據你之前(?:提供|給|貼|傳)的(?:文章|資料|內容)",
    r"我記得你之前(?:給過|提供過|傳過|說過)",
]

_STRONG_CLAIM_PATTERNS = [
    r"可以確認",
    r"我可以確定",
    r"毫無疑問",
    r"一定是",
    r"明確就是",
]


@dataclass()
class AnswerVerificationResult:
    passed: bool
    reason: str = ""
    safe_reply: str = ""
    issues: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _has_user_chatlog_support(memories: list[dict] | None, conversation_history: str = "") -> bool:
    if conversation_history and len(str(conversation_history).strip()) > 20:
        return True
    for memory in memories or []:
        if not isinstance(memory, dict):
            continue
        prov = parse_source_provenance(str(memory.get("source") or ""))
        if prov.source_type in {"chatlog", "user_chat"} and prov.role != "assistant":
            return True
    return False


def _contains_false_memory_claim(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in _FALSE_MEMORY_PATTERNS)


def _contains_overclaim(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in _STRONG_CLAIM_PATTERNS)


def verify_answer(
    *,
    query: str,
    answer: str,
    memories: list[dict] | None = None,
    memory_context: str = "",
    web_context: str = "",
    conversation_history: str = "",
) -> AnswerVerificationResult:
    text = str(answer or "").strip()
    if not text:
        return AnswerVerificationResult(
            passed=False,
            reason="empty_answer",
            safe_reply="目前沒有可驗證結果，請換一個更具體的事實點再試一次。",
            issues=["empty_answer"],
        )

    issues: list[str] = []
    if _contains_false_memory_claim(text) and not _has_user_chatlog_support(memories, conversation_history):
        issues.append("false_memory_claim_without_support")

    has_memory = bool(str(memory_context or "").strip() and str(memory_context).strip() != "無相關記憶。")
    has_web = bool(str(web_context or "").strip() and str(web_context).strip() not in {"無。", "無"})
    if _contains_overclaim(text) and not (has_memory or has_web):
        issues.append("overclaim_without_evidence")

    if issues:
        primary = issues[0]
        if primary == "false_memory_claim_without_support":
            safe_reply = "我目前沒有可驗證證據能證明你之前曾提供過那份內容，所以不應直接把它說成既有記憶。"
        else:
            safe_reply = "目前缺少足夠可驗證資料，我不應該把推測說成確定事實。"
        return AnswerVerificationResult(
            passed=False,
            reason=primary,
            safe_reply=safe_reply,
            issues=issues,
            metadata={
                "has_memory": has_memory,
                "has_web": has_web,
                "query": str(query or "")[:120],
            },
        )

    return AnswerVerificationResult(
        passed=True,
        reason="verified",
        safe_reply=text,
        metadata={
            "has_memory": has_memory,
            "has_web": has_web,
        },
    )

